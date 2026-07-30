# Multi-stage from day one — Phase 11 asks for a non-root image under ~300MB and
# retrofitting that onto a fat single-stage image is annoying.

FROM python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /bin/uv

WORKDIR /srv

# Dependency layer, cached independently of application source.
COPY pyproject.toml uv.lock* ./
RUN uv venv /srv/.venv && \
    uv sync --frozen --no-install-project --no-dev 2>/dev/null || \
    uv pip install --python /srv/.venv \
        "fastapi>=0.115" "uvicorn[standard]>=0.32" "pydantic>=2.9" \
        "pydantic-settings>=2.6" "psycopg[binary,pool]>=3.2" "redis>=5.2" \
        "structlog>=24.4" "alembic>=1.14" "sqlalchemy>=2.0"


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/srv/.venv/bin:$PATH"

RUN useradd --create-home --uid 10001 threadkeeper

WORKDIR /srv

COPY --from=builder /srv/.venv /srv/.venv
COPY --chown=threadkeeper:threadkeeper alembic.ini ./
COPY --chown=threadkeeper:threadkeeper migrations ./migrations
COPY --chown=threadkeeper:threadkeeper app ./app

USER threadkeeper

EXPOSE 8000

CMD ["uvicorn", "app.main:api", "--host", "0.0.0.0", "--port", "8000"]
