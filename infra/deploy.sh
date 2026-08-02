#!/usr/bin/env bash
#
# One command, from a clean checkout to a running deploy.
#
#     ./infra/deploy.sh
#
# What it does, in order: build the image for the architecture Fargate actually
# runs, push it to ECR under the git sha, apply the infrastructure, run the
# migrations as a one-shot task, then force a new deployment and wait for it.
#
# Idempotent. Running it twice with no code change is a no-op that still waits
# for the service to be stable, which is a useful thing to be able to do.

set -euo pipefail

cd "$(dirname "$0")"

REGION="${TK_AWS_REGION:-ap-south-1}"
PROJECT="${TK_PROJECT:-threadkeeper}"

# The git sha, not `latest`. The ECR repository is IMMUTABLE, so a tag identifies
# exactly one image forever — which is what makes "which code is running" a
# question with an answer. Refuses to deploy a dirty tree for the same reason:
# a sha that does not describe the running code is worse than no sha.
if ! git diff --quiet HEAD 2>/dev/null; then
  echo "working tree is dirty — commit first, or the image tag will lie about what is in it" >&2
  exit 1
fi
TAG="$(git rev-parse --short HEAD)"

step() { printf '\n\033[1;34m==>\033[0m %s\n' "$1"; }

step "Bootstrapping the registry"
# The repository has to exist before the image can be pushed, and the rest of
# the infrastructure wants the image URI — so this one resource is applied
# first, on its own, rather than splitting the module into two.
tofu init -input=false >/dev/null
tofu apply -input=false -auto-approve -target=aws_ecr_repository.main -var="image=bootstrap" >/dev/null
REGISTRY="$(tofu output -raw ecr_repository)"
IMAGE="${REGISTRY}:${TAG}"

step "Building ${IMAGE}"
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${REGISTRY%%/*}"

# --platform matters and is easy to get wrong on an Apple Silicon laptop: a
# native build produces arm64, Fargate's default runtime platform is X86_64, and
# the failure arrives as `exec format error` in a CloudWatch log rather than as
# a build error.
docker buildx build --platform linux/amd64 -t "$IMAGE" --push ..

step "Applying infrastructure"
tofu apply -input=false -auto-approve -var="image=${IMAGE}"

step "Checking the secrets are populated"
# Settings refuses to start without these, and the failure would otherwise be a
# task that crash-loops with a validation error nobody reads until the deploy
# has already been declared finished.
missing=()
for secret in vault-key customer-ref-secret whatsapp-app-secret gemini-api-key; do
  value="$(aws secretsmanager get-secret-value \
    --region "$REGION" --secret-id "${PROJECT}/${secret}" \
    --query SecretString --output text 2>/dev/null || true)"
  [[ -z "$value" || "$value" == "None" ]] && missing+=("${PROJECT}/${secret}")
done
if ((${#missing[@]})); then
  echo
  echo "these secrets are empty and the app will not start:" >&2
  printf '  %s\n' "${missing[@]}" >&2
  echo
  echo "populate them, then re-run. for example:" >&2
  echo "  aws secretsmanager put-secret-value --region $REGION \\" >&2
  echo "    --secret-id ${PROJECT}/vault-key --secret-string \"\$(python -c \\" >&2
  echo "    'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')\"" >&2
  exit 1
fi

step "Running migrations"
# A one-shot task rather than a container entrypoint: two api replicas starting
# together would otherwise race the same migration. Two steps, because
# PostgresSaver.setup() issues CREATE INDEX CONCURRENTLY, which deadlocks inside
# a migration transaction — see app/graph/checkpointer.py.
CLUSTER="$(tofu output -raw cluster)"
SUBNETS="$(tofu output -json | python3 -c 'import json,sys; print(",".join(json.load(sys.stdin)["subnets"]["value"]))')"
SG="$(tofu output -raw task_security_group)"

TASK_ARN="$(aws ecs run-task \
  --region "$REGION" --cluster "$CLUSTER" --launch-type FARGATE \
  --task-definition "${PROJECT}-api" \
  --network-configuration "awsvpcConfiguration={subnets=[${SUBNETS}],securityGroups=[${SG}],assignPublicIp=ENABLED}" \
  --overrides '{"containerOverrides":[{"name":"api","command":["sh","-c","alembic upgrade head && python -m app.graph.checkpointer"]}]}' \
  --query 'tasks[0].taskArn' --output text)"

aws ecs wait tasks-stopped --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK_ARN"
EXIT_CODE="$(aws ecs describe-tasks --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text)"
if [[ "$EXIT_CODE" != "0" ]]; then
  echo "migrations failed with exit code ${EXIT_CODE}" >&2
  echo "logs: aws logs tail /ecs/${PROJECT} --region $REGION --since 10m" >&2
  exit 1
fi

step "Rolling the services"
for service in api worker; do
  aws ecs update-service --region "$REGION" --cluster "$CLUSTER" \
    --service "${PROJECT}-${service}" --force-new-deployment >/dev/null
done

# This is where the drain happens. Old tasks get SIGTERM, finish their in-flight
# turns inside TK_DRAIN_TIMEOUT_S, and exit before ECS reaches stopTimeout.
aws ecs wait services-stable --region "$REGION" --cluster "$CLUSTER" \
  --services "${PROJECT}-api" "${PROJECT}-worker"

URL="$(tofu output -raw url)"
step "Deployed ${TAG}"
echo "  ${URL}/console"
echo "  ${URL}/health/ready"
echo
curl -fsS "${URL}/health/ready" && echo
