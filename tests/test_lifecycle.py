"""Graceful shutdown and in-flight drain.

The behaviour under test is the one a deploy exercises and nothing else does: a
customer's message is buffered, their reply has not been sent, and the container
is told to go away. Before this phase the answer was to cancel the turn, which
consumes the message and sends nothing — a reply silently lost on every deploy,
for whoever happened to be mid-conversation.

These tests drive the real buffer against the real stores, because the bug lived
in the interaction between a detached task and a shutdown hook, and neither half
is wrong on its own.
"""

from __future__ import annotations

import asyncio

import pytest

from app import lifecycle
from app.buffer import coalesce
from app.ingress import repository
from app.ingress.events import InboundEvent
from app.privacy.refs import customer_ref
from app.settings import get_settings

pytestmark = pytest.mark.integration


async def _push(cid: str, phone: str, text: str, msg_id: str) -> None:
    await coalesce.push(
        InboundEvent(
            channel="whatsapp",
            provider_msg_id=msg_id,
            customer_ref=customer_ref(phone),
            text=text,
            conversation_id=cid,
        )
    )


async def _outbound(cid: str) -> int:
    from app import db

    row = await db.fetch_one(
        "SELECT count(*) AS n FROM messages WHERE conversation_id = %s AND direction = 'out'",
        cid,
    )
    return int(row["n"]) if row else 0


@pytest.fixture(autouse=True)
def _reset_drain_flag() -> None:
    """Draining is process-global; a leaked flag would fail every later test."""
    lifecycle.reset_for_tests()


# ============================================================== THE FLAG
class TestDrainFlag:
    def test_it_starts_off(self) -> None:
        assert not lifecycle.is_draining()

    def test_begin_drain_is_idempotent(self) -> None:
        """SIGTERM twice — an impatient operator, or a signal plus a lifespan exit."""
        lifecycle.begin_drain()
        lifecycle.begin_drain()
        assert lifecycle.is_draining()


# ============================================================== THE PROBES
class TestProbes:
    async def test_ready_is_200_while_serving(self, live_app) -> None:  # type: ignore[no-untyped-def]
        resp = await live_app.get("/health/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_ready_turns_503_when_draining(self, live_app) -> None:  # type: ignore[no-untyped-def]
        lifecycle.begin_drain()
        resp = await live_app.get("/health/ready")
        assert resp.status_code == 503
        assert resp.json()["status"] == "draining"

    async def test_live_stays_200_when_draining(self, live_app) -> None:  # type: ignore[no-untyped-def]
        """The distinction the two probes exist for.

        A draining container is healthy — it is finishing work on purpose. Fail
        liveness here and the orchestrator kills it mid-turn, which is exactly
        what the drain was built to prevent.
        """
        lifecycle.begin_drain()
        resp = await live_app.get("/health/live")
        assert resp.status_code == 200

    async def test_live_touches_nothing_external(self, live_app, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """A Redis blip must not restart a healthy container."""
        from app import cache, db

        async def down() -> bool:
            raise AssertionError("liveness must not touch a dependency")

        monkeypatch.setattr(db, "ping", down)
        monkeypatch.setattr(cache, "ping", down)
        assert (await live_app.get("/health/live")).status_code == 200


# ============================================================== THE DRAIN
class TestDrain:
    async def test_an_in_flight_turn_is_finished_not_cancelled(
        self, live_db: None, clean_conversation: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole phase, in one assertion.

        A slow turn is in flight when shutdown begins. It must still send its
        reply. Before the fix this asserted zero — the task was cancelled, the
        message consumed, and the customer got nothing.
        """
        s = get_settings()
        monkeypatch.setattr(s, "buffer_window_s", 0.1)
        monkeypatch.setattr(s, "fake_turn_latency_s", 0.6)

        phone = clean_conversation
        conv = await repository.get_or_create_conversation("whatsapp", customer_ref(phone))
        cid = str(conv["id"])

        await _push(cid, phone, "personal loan chahiye", "wamid.drain.1")
        # Long enough for the window to close and the turn to be generating,
        # short enough that it is definitely not finished.
        await asyncio.sleep(0.3)

        drained = await coalesce.drain(timeout_s=15.0)

        assert drained["cancelled"] == 0, "a deploy must not cancel a customer's turn"
        assert drained["finished"] == 1
        assert await _outbound(cid) == 1, "the reply must actually have been sent"

    async def test_it_gives_up_after_the_timeout(
        self, live_db: None, clean_conversation: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bounded, because a hung turn must not hold the container open.

        The orchestrator's SIGKILL would cancel everything anyway, with no log
        line explaining it. Waiting a known number of seconds and then saying
        what was abandoned is strictly better than being killed.
        """
        s = get_settings()
        monkeypatch.setattr(s, "buffer_window_s", 0.1)
        monkeypatch.setattr(s, "fake_turn_latency_s", 30.0)

        phone = clean_conversation
        conv = await repository.get_or_create_conversation("whatsapp", customer_ref(phone))
        cid = str(conv["id"])

        await _push(cid, phone, "personal loan chahiye", "wamid.drain.2")
        await asyncio.sleep(0.3)

        drained = await coalesce.drain(timeout_s=0.5)
        assert drained["cancelled"] == 1
        assert drained["finished"] == 0

    async def test_draining_nothing_is_not_an_error(self, live_db: None) -> None:
        assert await coalesce.drain(timeout_s=5.0) == {"finished": 0, "cancelled": 0}

    async def test_the_drain_takes_no_longer_than_it_needs_to(
        self, live_db: None, clean_conversation: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It must return when the work is done, not sleep out the timeout.

        A drain that always waits the full window turns every deploy into a
        25-second pause per task, which is how a timeout becomes a rollout
        duration nobody can explain.
        """
        s = get_settings()
        monkeypatch.setattr(s, "buffer_window_s", 0.1)
        monkeypatch.setattr(s, "fake_turn_latency_s", 0.3)

        phone = clean_conversation
        conv = await repository.get_or_create_conversation("whatsapp", customer_ref(phone))
        cid = str(conv["id"])

        await _push(cid, phone, "personal loan chahiye", "wamid.drain.3")
        await asyncio.sleep(0.2)

        started = asyncio.get_running_loop().time()
        await coalesce.drain(timeout_s=20.0)
        assert asyncio.get_running_loop().time() - started < 5.0

    async def test_shutdown_is_still_the_impatient_version(
        self, live_db: None, clean_conversation: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ctrl-C locally should not wait. Only a deploy drains."""
        s = get_settings()
        monkeypatch.setattr(s, "buffer_window_s", 0.1)
        monkeypatch.setattr(s, "fake_turn_latency_s", 30.0)

        phone = clean_conversation
        conv = await repository.get_or_create_conversation("whatsapp", customer_ref(phone))
        cid = str(conv["id"])

        await _push(cid, phone, "personal loan chahiye", "wamid.drain.4")
        await asyncio.sleep(0.3)

        started = asyncio.get_running_loop().time()
        await coalesce.shutdown()
        assert asyncio.get_running_loop().time() - started < 2.0

    async def test_a_newer_message_still_cancels_the_older_turn(
        self, live_db: None, clean_conversation: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The case cancellation was designed for, which must survive this change.

        A SIGTERM is not a newer message. A newer message is — and there the
        customer does get a reply, to the thing they actually said last.
        """
        s = get_settings()
        monkeypatch.setattr(s, "buffer_window_s", 0.15)
        monkeypatch.setattr(s, "fake_turn_latency_s", 0.5)

        phone = clean_conversation
        conv = await repository.get_or_create_conversation("whatsapp", customer_ref(phone))
        cid = str(conv["id"])

        await _push(cid, phone, "personal loan chahiye", "wamid.drain.5")
        await asyncio.sleep(0.3)
        await _push(cid, phone, "actually 5 lakh chahiye", "wamid.drain.6")

        await coalesce.drain(timeout_s=15.0)
        assert await _outbound(cid) == 1, "one reply, to the newer message"


# ============================================================== THE TIMEOUT LADDER
class TestTimeoutsAgree:
    """Three timeouts in three files that only work if they increase in order.

    TK_DRAIN_TIMEOUT_S < uvicorn --timeout-graceful-shutdown < ECS stop_timeout.
    Get it backwards and the drain is cut off by the thing that was supposed to
    be waiting for it — silently, because everything still "works", just with a
    lost reply on every deploy.
    """

    def _numbers(self) -> tuple[float, float, float]:
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        dockerfile = (root / "Dockerfile").read_text()
        uvicorn_s = float(
            re.search(r'"--timeout-graceful-shutdown",\s*\\?\s*"(\d+)"', dockerfile).group(1)
        )

        terraform = (root / "infra" / "ecs.tf").read_text()
        stop_timeout = float(re.search(r"stopTimeout\s*=\s*(\d+)", terraform).group(1))

        return get_settings().drain_timeout_s, uvicorn_s, stop_timeout

    def test_they_increase(self) -> None:
        drain, uvicorn_s, stop_timeout = self._numbers()
        assert drain < uvicorn_s < stop_timeout, (
            f"drain={drain} uvicorn={uvicorn_s} ecs={stop_timeout}"
        )

    def test_ecs_stop_timeout_is_within_the_aws_maximum(self) -> None:
        """ECS caps stopTimeout at 120s; a larger value is silently ignored."""
        _, _, stop_timeout = self._numbers()
        assert stop_timeout <= 120
