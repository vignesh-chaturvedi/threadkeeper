"""FastAPI application factory.

Phase 00 ships exactly two things worth having on day one: a liveness probe that
never touches a dependency, and a readiness probe that touches all of them. The
distinction matters later — Phase 11 puts a load balancer in front of this, and a
liveness check that fails because Redis blipped will restart a healthy container.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app import cache, db
from app.ingress import simulator, webhook
from app.logging import bind_contextvars, clear_contextvars, configure_logging, get_logger
from app.settings import get_settings

log = get_logger(__name__)

STARTED_AT = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()
    log.info("startup", env=settings.env, version=app.version)
    await db.open_pool()
    await cache.open_redis()
    try:
        yield
    finally:
        await cache.close_redis()
        await db.close_pool()
        log.info("shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Threadkeeper",
        version="0.1.0",
        description="A durable, resumable conversational sales agent for an Indian lending funnel.",
        lifespan=lifespan,
        debug=settings.debug,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Every request gets a request_id; every log line inside it inherits one.

        Phase 01 binds conversation_id here too, once the webhook payload is parsed.
        """
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        clear_contextvars()
        bind_contextvars(request_id=request_id, path=request.url.path)
        started = time.perf_counter()
        try:
            response: Response = await call_next(request)
        except Exception:
            log.exception("request_failed", method=request.method)
            raise
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        # Health probes fire constantly; logging them drowns everything else.
        if not request.url.path.startswith("/health"):
            log.info(
                "request",
                method=request.method,
                status=response.status_code,
                elapsed_ms=elapsed_ms,
            )
        response.headers["x-request-id"] = request_id
        return response

    @app.get("/health/live", tags=["health"])
    async def live() -> dict[str, object]:
        """Liveness: is the process alive? Touches nothing external, by design."""
        return {"status": "ok", "uptime_s": round(time.time() - STARTED_AT, 1)}

    @app.get("/health/ready", tags=["health"])
    async def ready() -> JSONResponse:
        """Readiness: can this process actually serve a turn right now?"""
        checks = {"postgres": await db.ping(), "redis": await cache.ping()}
        healthy = all(checks.values())
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={"status": "ok" if healthy else "degraded", "checks": checks},
        )

    @app.get("/", tags=["meta"])
    async def root() -> dict[str, str]:
        return {"service": "threadkeeper", "version": app.version, "docs": "/docs"}

    app.include_router(webhook.router)

    # The simulator is a development affordance, not a feature. Settings force
    # it off outside local/test; this is the second lock on the same door.
    if settings.enable_simulator and settings.env in ("local", "test"):
        app.include_router(simulator.router)
        log.info("simulator_mounted", path="/sim")

    return app


api = create_app()
