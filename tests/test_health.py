"""Phase 00 acceptance: the app boots, probes answer, and readiness tells the
truth about its dependencies rather than optimistically returning 200.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app import cache, db
from app.main import create_app


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    """Runs create_app() without a live Postgres/Redis by stubbing the probes.

    The integration test below covers the real thing.
    """
    app = create_app()
    app.router.lifespan_context = _noop_lifespan

    async def ok() -> bool:
        return True

    monkeypatch.setattr(db, "ping", ok)
    monkeypatch.setattr(cache, "ping", ok)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _noop_lifespan(app):  # type: ignore[no-untyped-def]
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _ctx():
        yield

    return _ctx()


async def test_liveness_never_touches_dependencies(client: AsyncClient) -> None:
    r = await client.get("/health/live")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "uptime_s" in r.json()


async def test_readiness_reports_each_dependency(client: AsyncClient) -> None:
    r = await client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["checks"] == {"postgres": True, "redis": True}


async def test_readiness_is_503_when_a_dependency_is_down(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A degraded process must not be sent traffic. 200-always is a common bug."""

    async def down() -> bool:
        return False

    monkeypatch.setattr(cache, "ping", down)

    r = await client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"
    assert r.json()["checks"]["redis"] is False


async def test_request_id_is_echoed(client: AsyncClient) -> None:
    """Phase 01 relies on this header to correlate a webhook with its logs."""
    r = await client.get("/", headers={"x-request-id": "abc123"})
    assert r.headers["x-request-id"] == "abc123"
