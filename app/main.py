"""FastAPI application factory.

Phase 00 ships exactly two things worth having on day one: a liveness probe that
never touches a dependency, and a readiness probe that touches all of them. The
distinction matters later — Phase 11 puts a load balancer in front of this, and a
liveness check that fails because Redis blipped will restart a healthy container.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app import cache, db, lifecycle
from app.buffer import coalesce
from app.ingress import simulator, webhook
from app.logging import bind_contextvars, clear_contextvars, configure_logging, get_logger
from app.obs import console
from app.scheduler import worker
from app.settings import get_settings

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()
    log.info("startup", env=settings.env, version=app.version)
    await db.open_pool()
    await cache.open_redis()

    # Single-process deployments run the scheduler here. The loop owns no stores
    # of its own — this lifespan opened them and will close them — so it is the
    # bare poll loop rather than the standalone entrypoint.
    worker_task: asyncio.Task[None] | None = None
    if settings.run_worker_in_process:
        worker_task = asyncio.create_task(worker.poll_loop(), name="followup-worker")
        log.info("worker_in_process", poll_interval_s=settings.scheduler_poll_interval_s)

    try:
        yield
    finally:
        # Order matters, and it is the reverse of startup for a reason: the
        # turns being drained are still using both stores, so closing the pool
        # first would fail every turn we are trying to let finish.
        lifecycle.begin_drain()

        # Stop claiming new follow-ups before draining turns. The other order
        # lets the worker claim a job during the drain and then lose it to the
        # store closing underneath it — a nudge marked 'running' that never ran.
        if worker_task is not None:
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task

        drained = await coalesce.drain(settings.drain_timeout_s)
        await cache.close_redis()
        await db.close_pool()
        log.info("shutdown", **drained)


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
        """Liveness: is the process alive? Touches nothing external, by design.

        Stays 200 while draining. A draining container is healthy — it is
        finishing work on purpose — and failing liveness here would have the
        orchestrator kill it mid-turn, which is the precise outcome the drain
        exists to avoid.
        """
        return {"status": "ok", "uptime_s": round(lifecycle.uptime(), 1)}

    @app.get("/health/ready", tags=["health"])
    async def ready() -> JSONResponse:
        """Readiness: should this process receive new work right now?"""
        if lifecycle.is_draining():
            return JSONResponse(
                status_code=503,
                content={"status": "draining", "checks": {}},
            )
        checks = {"postgres": await db.ping(), "redis": await cache.ping()}
        healthy = all(checks.values())
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={"status": "ok" if healthy else "degraded", "checks": checks},
        )

    @app.get("/", tags=["meta"])
    async def root() -> dict[str, str]:
        return {"service": "threadkeeper", "version": app.version, "docs": "/docs"}

    @app.get("/console/escalations", tags=["console"])
    async def escalations(limit: int = 50) -> dict[str, object]:
        """The human queue. Phase 06 puts a UI on this; the packet is the product."""
        from app.graph import escalation

        queue = await escalation.open_queue(limit)
        return {"open": len(queue), "escalations": queue}

    app.include_router(webhook.router)
    # The console reads; it never writes. Mounted in every environment because
    # "what is the funnel doing" is a production question, not a dev affordance —
    # unlike the simulator below, which forges inbound traffic.
    app.include_router(console.router)

    # The simulator is a development affordance, not a feature — it forges
    # signed inbound traffic. `mount_simulator` holds the whole rule, including
    # the deliberate demo-sandbox opt-in; duplicating any part of it here is how
    # the two copies would drift.
    if settings.mount_simulator:
        app.include_router(simulator.router)
        log.info("simulator_mounted", path="/sim", demo_sandbox=settings.demo_sandbox)
        if settings.demo_sandbox:
            log.warning(
                "public_simulator_enabled",
                detail="/sim forges inbound turns and spends model quota",
                env=settings.env,
            )

    return app


api = create_app()
