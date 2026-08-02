# Two services on one cluster: api behind the load balancer, worker behind
# nothing. They share an image and a task role and differ only in their command
# and whether traffic reaches them.

resource "aws_ecr_repository" "main" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"

  # A tag that can be moved is a deploy that cannot be described. IMMUTABLE plus
  # the git sha as the tag means "which code is running" has an answer.
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecs_cluster" "main" {
  name = local.name
}

resource "aws_cloudwatch_log_group" "main" {
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days
}

# ------------------------------------------------------------------- iam
# Two roles, because they are trusted by different things. The execution role is
# used by the ECS agent before the container starts — pulling the image, reading
# the secrets it injects. The task role is what the application itself gets. The
# application never needs to read a secret at runtime (they arrive as env vars),
# so its role grants nothing at all, and that is deliberate rather than unfinished.

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_secrets" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.database_url.arn,
      aws_secretsmanager_secret.vault_key.arn,
      aws_secretsmanager_secret.customer_ref_secret.arn,
      aws_secretsmanager_secret.whatsapp_app_secret.arn,
      aws_secretsmanager_secret.gemini_api_key.arn,
    ]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "${local.name}-read-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

# ------------------------------------------------------------- task definition
locals {
  secrets = [
    { name = "TK_DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
    { name = "TK_VAULT_KEY", valueFrom = aws_secretsmanager_secret.vault_key.arn },
    { name = "TK_CUSTOMER_REF_SECRET", valueFrom = aws_secretsmanager_secret.customer_ref_secret.arn },
    { name = "TK_WHATSAPP_APP_SECRET", valueFrom = aws_secretsmanager_secret.whatsapp_app_secret.arn },
    { name = "TK_GEMINI_API_KEY", valueFrom = aws_secretsmanager_secret.gemini_api_key.arn },
  ]

  environment = [
    { name = "TK_ENV", value = "prod" },
    { name = "TK_LOG_FORMAT", value = "json" },
    { name = "TK_REDIS_URL", value = "redis://${aws_elasticache_cluster.main.cache_nodes[0].address}:6379/0" },
    { name = "TK_LLM_PROVIDER", value = "gemini" },
    { name = "TK_OUTBOUND_TRANSPORT", value = "mock" },
    { name = "TK_WHATSAPP_VERIFY_TOKEN", value = var.whatsapp_verify_token },
    # Read with local.stop_timeout_s in main.tf and the uvicorn flag in the
    # Dockerfile: 25 < 30 < 40. A test asserts the ordering across all three.
    { name = "TK_DRAIN_TIMEOUT_S", value = "25" },
    # Belt and braces: Settings already forces this off when env is prod.
    { name = "TK_ENABLE_SIMULATOR", value = "false" },
  ]

  log_configuration = {
    logDriver = "awslogs"
    options = {
      "awslogs-group"         = aws_cloudwatch_log_group.main.name
      "awslogs-region"        = var.region
      "awslogs-stream-prefix" = "ecs"
    }
  }
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name        = "api"
    image       = var.image
    essential   = true
    environment = local.environment
    secrets     = local.secrets

    portMappings = [{ containerPort = 8000, protocol = "tcp" }]

    # The outer bound of the drain contract. ECS sends SIGTERM, waits this long,
    # then SIGKILL. It must exceed uvicorn's graceful-shutdown timeout (30s),
    # which must exceed TK_DRAIN_TIMEOUT_S (25s) — otherwise the thing meant to
    # be waiting for the drain is what cuts it off, silently, and every deploy
    # drops a reply for whoever was mid-conversation.
    stopTimeout = 40

    # A container-level check as well as the ALB's, because they answer for
    # different failures: this one catches a task whose process is wedged but
    # whose port still accepts connections, and it works for the worker too,
    # which has no load balancer in front of it at all.
    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3).status==200 else 1)\""]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 30
    }

    logConfiguration = local.log_configuration
  }])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name        = "worker"
    image       = var.image
    essential   = true
    command     = ["python", "-m", "app.scheduler.worker"]
    environment = local.environment
    secrets     = local.secrets

    # The worker already handles SIGTERM: it sets a stop event and finishes the
    # batch it claimed rather than abandoning jobs mid-send. A claimed follow-up
    # that dies unsent stays 'running' in Postgres until reconcile picks it up,
    # so nothing is lost either way — this just makes the common case clean.
    stopTimeout = 40

    logConfiguration = local.log_configuration
  }])
}

# ------------------------------------------------------------------- services
resource "aws_ecs_service" "api" {
  name            = "${local.name}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = true # no NAT gateway; see network.tf
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  # 100/50 rather than the default 200/100: start every replacement before
  # stopping anything, and never drop below half. With desired_count = 2 that
  # means one task drains at a time while the other serves.
  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 100

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # A new task must pass its health check for this long before ECS counts the
  # deployment as progressing. Longer than the app's cold start, or a task that
  # is still importing langgraph gets marked unhealthy and replaced, forever.
  health_check_grace_period_seconds = 60

  depends_on = [aws_lb_listener.http]
}

resource "aws_ecs_service" "worker" {
  name            = "${local.name}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = true
  }

  # No load balancer, so no minimum healthy percent worth setting: a moment with
  # zero workers delays a nudge, it does not lose one. The ZSET is rebuilt from
  # Postgres on start.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
}
