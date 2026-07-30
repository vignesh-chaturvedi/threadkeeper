# Multi-stage from day one — Phase 11 asks for a non-root image under ~300MB and
# retrofitting that onto a fat single-stage image is annoying.

FROM python:3.12-slim AS builder

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
COPY pyproject.toml uv.lock ./
RUN uv venv /srv/.venv && uv sync --frozen --no-install-project --no-dev


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
