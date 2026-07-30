"""Shared fixtures.

Integration tests here talk to the real Postgres from docker compose. They are
marked `integration` and skipped automatically when it isn't reachable, so
`uv run pytest` stays green on a laptop with nothing running while CI (Phase 08)
gets the full suite.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

# Must be set before app.settings is imported anywhere.
os.environ.setdefault("TK_ENV", "test")
os.environ.setdefault("TK_WHATSAPP_APP_SECRET", "test-app-secret")
os.environ.setdefault("TK_CUSTOMER_REF_SECRET", "test-ref-secret")
os.environ.setdefault(
    "TK_DATABASE_URL", "postgresql://threadkeeper:dev@localhost:5433/threadkeeper"
)
os.environ.setdefault("TK_REDIS_URL", "redis://localhost:6379/0")

from app import cache, db
from app.main import create_app
from app.settings import get_settings


async def _postgres_available() -> bool:
    try:
        await db.open_pool()
        return await db.ping()
    except Exception:  # noqa: BLE001 — availability probe, any failure means skip
        return False


@pytest.fixture(scope="session")
def settings():  # type: ignore[no-untyped-def]
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
async def live_db() -> AsyncIterator[None]:
    """Opens both stores, or skips the test if Postgres isn't up.

    Redis comes along because since Phase 02 they are not separable: the turn
    buffer keeps its generation counter and debounce deadlines there, so any
    test touching a conversation touches both.
    """
    if not await _postgres_available():
        pytest.skip("postgres not reachable — run `docker compose up -d db redis`")
    await cache.open_redis()
    yield
    # Settle tasks are detached and still touching Redis; drain them before the
    # pool goes away, exactly as the real lifespan does.
    from app.buffer import coalesce

    await coalesce.shutdown()
    await cache.close_redis()
    await db.close_pool()


@pytest.fixture
async def live_app(live_db: None) -> AsyncIterator[object]:
    """The real app against real stores, without uvicorn."""
    from httpx import ASGITransport, AsyncClient

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def clean_conversation(live_db: None) -> AsyncIterator[str]:
    """A phone number whose conversation is deleted before and after the test."""
    from app.privacy.refs import customer_ref

    phone = "919000000001"
    ref = customer_ref(phone)

    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)
    yield phone
    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)


@pytest.fixture(autouse=True)
def fast_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the debounce windows suite-wide.

    Production defaults are 2.5s / 8s. Left alone, every integration test would
    pay that, and the suite would take minutes instead of seconds. Tests that
    care about a specific timing relationship override these again locally.
    """
    s = get_settings()
    monkeypatch.setattr(s, "buffer_window_s", 0.3)
    monkeypatch.setattr(s, "buffer_max_hold_s", 1.2)
    monkeypatch.setattr(s, "fake_turn_latency_s", 0.0)
