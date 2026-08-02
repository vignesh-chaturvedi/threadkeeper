# Multi-stage, non-root, Alpine base. Phase 11 asks for ~300MB; the Debian slim
# version of this file measured 382MB after stripping, and 205MB of that was the
# base image alone. Every wheel this project needs publishes a musl build, so
# the base is the thing worth changing rather than the dependencies.

FROM python:3.12-alpine AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /bin/uv

WORKDIR /srv

# Dependency layer, cached independently of application source.
#
# --frozen means the lockfile is the contract: if pyproject.toml gained a
# dependency that uv.lock doesn't have, this fails the build. An earlier version
# of this file fell back to a hardcoded pip install on error, which silently
# produced an image missing a newly added package — the failure surfaced as a
# ModuleNotFoundError at container start instead. No fallback now, on purpose.
#
# It is also what makes the Alpine base safe: if any dependency stopped shipping
# a musl wheel, this fails loudly at build time rather than quietly compiling
# from source and doubling the image.
COPY pyproject.toml uv.lock ./
RUN uv venv /srv/.venv && uv sync --frozen --no-install-project --no-dev

# Slim the virtualenv. Measured, not assumed.
#
# Stripping debug symbols out of the compiled extensions is the biggest single
# win — cryptography, psycopg_binary, pydantic_core and uvloop ship ~73MB of
# unstripped .so files, and those symbols are no use in a container nobody will
# attach a debugger to. Bytecode is deliberately KEPT, at ~42MB: it buys a
# faster cold start, and on Fargate a slow start is time a new task spends
# failing its health check while the load balancer holds traffic back.
RUN set -eux; \
    apk add --no-cache binutils; \
    find /srv/.venv -name '*.so' -exec strip --strip-unneeded {} + ; \
    find /srv/.venv -type d -name 'tests' -prune -exec rm -rf {} + ; \
    rm -rf /srv/.venv/lib/python3.12/site-packages/pip \
           /srv/.venv/lib/python3.12/site-packages/setuptools \
           /srv/.venv/lib/python3.12/site-packages/pkg_resources


FROM python:3.12-alpine AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/srv/.venv/bin:$PATH"

# No cleanup of the base image here on purpose: deleting files from a base layer
# in a later layer does not shrink an image, it writes a whiteout on top and
# makes it marginally bigger. The Debian version of this file had exactly that
# mistake, trimming 10MB of idlelib and pip for no gain at all.
RUN adduser --disabled-password --uid 10001 threadkeeper

WORKDIR /srv

COPY --from=builder /srv/.venv /srv/.venv
COPY --chown=threadkeeper:threadkeeper alembic.ini ./
COPY --chown=threadkeeper:threadkeeper migrations ./migrations
COPY --chown=threadkeeper:threadkeeper app ./app

USER threadkeeper

EXPOSE 8000

# Exec form, so uvicorn is PID 1 and receives SIGTERM directly. The shell form
# would put /bin/sh at PID 1, and sh does not forward signals to its child — the
# container would sit until the orchestrator gave up and sent SIGKILL, killing
# every in-flight turn. The whole drain, defeated by a pair of quotes.
#
# Three timeouts that must increase in this order, or the drain never finishes:
#   TK_DRAIN_TIMEOUT_S  25s   how long we wait for in-flight turns
#   graceful-shutdown   30s   uvicorn abandons the lifespan shutdown here
#   ECS stop_timeout    40s   SIGKILL
CMD ["uvicorn", "app.main:api", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-graceful-shutdown", "30"]
