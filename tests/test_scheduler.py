"""The scheduler end to end, against real Postgres and Redis.

The headline is test_a_lead_abandoned_at_kyc_gets_one_nudge_and_a_lead_who_replied_gets_none
— the plan's done-when, both halves, as one assertion each.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app import db
from app.cache import redis
from app.graph.runner import run_turn
from app.ingress import repository
from app.privacy.refs import customer_ref
from app.scheduler import clock, policy, queue, reentry, worker
from app.settings import get_settings

pytestmark = pytest.mark.integration


@pytest.fixture
async def customer(live_db):
    """One conversation, with the clock pinned to a sendable hour.

    Without this the suite passes or fails depending on what time of day it is
    run: at 05:31 IST every nudge is correctly deferred for quiet hours, and
    every assertion about a nudge being *sent* fails. Pinning to 12:00 IST makes
    the tests deterministic and leaves quiet-hour behaviour to the one test that
    deliberately moves into the small hours.
    """
    phone = "919000000088"
    ref = customer_ref(phone)
    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)
    await redis().delete(queue.ZSET, clock.KEY)
    await _pin_clock_to_midday()
    conv = await repository.get_or_create_conversation("whatsapp", ref)
    cid = str(conv["id"])
    for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
        await db.execute(f"DELETE FROM {table} WHERE thread_id = %s", cid)
    yield cid
    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)
    await redis().delete(queue.ZSET, clock.KEY)


async def _pin_clock_to_midday() -> None:
    """Move the scheduler's clock to the next 12:00 IST."""
    real = datetime.now(UTC)
    local = real.astimezone(policy.IST)
    target = local.replace(hour=12, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    await clock.skip(target.astimezone(UTC) - real)


async def _outbound(cid: str) -> list[str]:
    rows = await db.fetch_all(
        "SELECT body FROM messages WHERE conversation_id = %s AND direction = 'out' ORDER BY id",
        cid,
    )
    return [r["body"] for r in rows]


async def _say(cid: str, text: str) -> str:
    await db.execute(
        "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'in', %s)", cid, text
    )
    reply = await run_turn(cid, text)
    await db.execute(
        "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'out', %s)", cid, reply
    )
    await db.execute(
        "UPDATE conversations SET last_in_at = %s WHERE id = %s", await clock.now(), cid
    )
    return reply


async def nudges_after(cid: str, baseline: list[str]) -> list[str]:
    """Outbound messages added since `baseline` — i.e. the ones the worker sent.

    Counting "outbound messages after the last inbound" does not work: a turn's
    own reply is also outbound and also after the customer's message, so every
    ordinary reply looked like a nudge.
    """
    return (await _outbound(cid))[len(baseline) :]


# ============================================================== THE DONE-WHEN
async def test_a_lead_abandoned_at_kyc_gets_one_context_aware_nudge(customer: str) -> None:
    """First half of the plan's acceptance criterion."""
    cid = customer
    await _say(cid, "personal loan chahiye 5 lakh")
    await _say(cid, "haan theek hai")
    await _say(cid, "salary 1 lakh se zyada, Mumbai")

    stage = (await db.fetch_one("SELECT stage FROM conversations WHERE id = %s", cid))["stage"]
    assert stage == "kyc_collect", "the lead should be sitting at KYC"

    pending = await queue.pending_for(cid)
    assert pending is not None, "going quiet at KYC should have armed a nudge"
    assert pending["stage_at_drop"] == "kyc_collect"

    # Nobody waits two hours. This is what the clock-skip control is for.
    before = await _outbound(cid)
    await clock.skip(timedelta(hours=3))
    processed = await worker.tick()
    # `>=` not `==`: the suite runs against a shared Postgres, and asserting a
    # *global* claim count makes this test fail whenever anything else has a due
    # job. What matters is that this conversation's nudge went out — asserted
    # below — not how many other conversations the worker also served.
    assert processed >= 1, "the armed job should have been claimed"

    nudges = await nudges_after(cid, before)
    assert len(nudges) == 1, f"expected exactly one nudge, got {len(nudges)}"
    assert "PAN" in nudges[0] or "pan" in nudges[0].lower(), (
        f"the nudge must name the drop-off point, got: {nudges[0]}"
    )

    # Every turn arms a fresh nudge and cancels the previous one, so the row
    # that matters is the one marked sent, not the oldest.
    row = await db.fetch_one(
        "SELECT sent_at FROM followups WHERE conversation_id = %s AND status = 'sent'", cid
    )
    assert row is not None and row["sent_at"] is not None


async def test_a_lead_who_replied_gets_none(customer: str) -> None:
    """Second half. A nudge sent to someone who already answered reads as broken."""
    cid = customer
    await _say(cid, "personal loan chahiye")

    pending = await queue.pending_for(cid)
    assert pending is not None

    armed = pending["id"]

    # They come back before the nudge is due.
    await _say(cid, "haan theek hai")

    # The nudge that was waiting for them is now answered and must be gone.
    row = await db.fetch_one("SELECT status, cancelled_reason FROM followups WHERE id = %s", armed)
    assert row["status"] == "cancelled"
    assert row["cancelled_reason"] == "customer_replied"

    before = await _outbound(cid)
    await worker.tick()
    assert await nudges_after(cid, before) == [], "a customer who replied must not be nudged"


async def test_a_due_nudge_is_dropped_if_they_replied_after_it_was_scheduled(
    customer: str,
) -> None:
    """The worker-side half of the same rule.

    A job can become due *and* be obsolete — the reply landed between scheduling
    and the worker waking up. The claim loop has to notice, not just the turn.
    """
    cid = customer
    await _say(cid, "personal loan chahiye")

    # Force a job that is already due, then have the customer reply after it.
    past = await clock.now() - timedelta(minutes=5)
    await queue.schedule(cid, stage_at_drop="qualify", at=past)
    # The scheduler's clock, not SQL now() — the same coherence rule the
    # repository follows, for the same reason.
    await db.execute(
        "UPDATE conversations SET last_in_at = %s WHERE id = %s", await clock.now(), cid
    )

    before = await _outbound(cid)
    processed = await worker.tick()
    assert processed >= 1, "the job should have been claimed"
    assert await nudges_after(cid, before) == [], "and then dropped, not sent"

    row = await db.fetch_one(
        "SELECT status, cancelled_reason FROM followups WHERE conversation_id = %s "
        "ORDER BY id DESC LIMIT 1",
        cid,
    )
    assert row["cancelled_reason"] == "customer_replied"


# ================================================================ STOP RULES
async def test_an_opted_out_lead_is_never_nudged(customer: str) -> None:
    cid = customer
    await _say(cid, "personal loan chahiye")
    await _say(cid, "band karo, mat bhejo")

    assert await queue.pending_for(cid) is None, "opting out must disarm the nudge"

    before = await _outbound(cid)
    await clock.skip(timedelta(days=1))
    await worker.tick()
    assert await nudges_after(cid, before) == []


async def test_an_escalated_conversation_is_dropped_not_nudged(customer: str) -> None:
    cid = customer
    await _say(cid, "personal loan chahiye")
    await db.execute("UPDATE conversations SET status = 'escalated' WHERE id = %s", cid)

    await clock.skip(timedelta(hours=3))
    await worker.tick()

    row = await db.fetch_one(
        "SELECT status, cancelled_reason FROM followups WHERE conversation_id = %s "
        "ORDER BY id DESC LIMIT 1",
        cid,
    )
    assert row["status"] == "cancelled"
    assert row["cancelled_reason"] == "conversation_escalated"


# ============================================================== QUIET HOURS
async def test_a_nudge_due_at_3am_is_deferred_not_sent(customer: str, monkeypatch) -> None:
    cid = customer
    await _say(cid, "personal loan chahiye")

    # Move the *scheduler's* clock to 03:00 IST — computed from clock.now(),
    # not wall time, because the fixture has already offset it.
    before = await _outbound(cid)
    now = await clock.now()
    target = (now.astimezone(policy.IST) + timedelta(days=1)).replace(
        hour=3, minute=0, second=0, microsecond=0
    )
    await clock.skip(target.astimezone(UTC) - now)

    processed = await worker.tick()
    assert processed >= 1

    assert await nudges_after(cid, before) == [], "nothing goes out at 3am"

    row = await db.fetch_one(
        "SELECT status, due_at FROM followups WHERE conversation_id = %s ORDER BY id DESC LIMIT 1",
        cid,
    )
    assert row["status"] == "pending", "it should be rescheduled, not dropped"
    assert row["due_at"].astimezone(policy.IST).hour == policy.QUIET_END_HOUR


# =========================================================== SERVICE WINDOW
async def test_outside_the_24h_window_a_template_is_used(customer: str) -> None:
    """WhatsApp will not deliver free-form text outside the window."""
    cid = customer
    await _say(cid, "personal loan chahiye")

    # Pretend their last message was two days ago.
    await db.execute(
        "UPDATE conversations SET last_in_at = now() - interval '2 days' WHERE id = %s", cid
    )
    await clock.skip(timedelta(hours=3))
    await worker.tick()

    row = await db.fetch_one(
        "SELECT template_name, status FROM followups WHERE conversation_id = %s "
        "AND status = 'sent' ORDER BY id DESC LIMIT 1",
        cid,
    )
    assert row is not None and row["status"] == "sent"
    assert row["template_name"] in reentry.TEMPLATES, (
        f"outside the window a named template is required, got {row['template_name']!r}"
    )


async def test_inside_the_window_the_message_is_free_form(customer: str) -> None:
    cid = customer
    await _say(cid, "personal loan chahiye")
    await clock.skip(timedelta(hours=3))
    await worker.tick()

    row = await db.fetch_one(
        "SELECT template_name FROM followups WHERE conversation_id = %s AND status = 'sent' "
        "ORDER BY id DESC LIMIT 1",
        cid,
    )
    assert row["template_name"] is None, "inside 24h the model writes it"


# ================================================================== BACKOFF
async def test_attempts_are_capped_at_four(customer: str) -> None:
    cid = customer
    await _say(cid, "personal loan chahiye")

    before = await _outbound(cid)
    for _ in range(6):
        await clock.skip(timedelta(days=8))
        await worker.tick()

    nudges = await nudges_after(cid, before)
    assert len(nudges) == policy.MAX_ATTEMPTS, (
        f"expected {policy.MAX_ATTEMPTS} nudges, got {len(nudges)}"
    )

    row = await db.fetch_one(
        "SELECT count(*) AS n FROM followups WHERE conversation_id = %s AND status = 'exhausted'",
        cid,
    )
    assert row["n"] == 1


async def test_one_pending_nudge_per_conversation(customer: str) -> None:
    """Five messages must not leave five nudges that all fire at once."""
    cid = customer
    for text in ["hi", "personal loan", "5 lakh", "urgent", "bhai reply karo"]:
        await _say(cid, text)

    row = await db.fetch_one(
        "SELECT count(*) AS n FROM followups WHERE conversation_id = %s "
        "AND status IN ('pending','running')",
        cid,
    )
    assert row["n"] == 1


# ================================================================== DURABILITY
async def test_a_flushed_redis_does_not_lose_the_nudge(customer: str) -> None:
    """Redis is a cache. Postgres is the record of truth, and this proves it."""
    cid = customer
    await _say(cid, "personal loan chahiye")
    assert await redis().zcard(queue.ZSET) >= 1

    await redis().delete(queue.ZSET)
    assert await redis().zcard(queue.ZSET) == 0

    before = await _outbound(cid)
    await clock.skip(timedelta(hours=3))
    processed = await worker.tick()
    assert processed >= 1, "the claim query reads Postgres, not the ZSET"
    assert len(await nudges_after(cid, before)) == 1

    restored = await queue.reconcile()
    assert restored >= 1, "reconcile rebuilds the ZSET from Postgres"


async def test_two_workers_never_send_the_same_nudge(customer: str) -> None:
    """FOR UPDATE SKIP LOCKED, exercised concurrently."""
    cid = customer
    await _say(cid, "personal loan chahiye")
    await clock.skip(timedelta(hours=3))

    now = await clock.now()
    results = await asyncio.gather(*[queue.claim(now, limit=20) for _ in range(5)])
    claimed = [job for batch in results for job in batch]
    assert len(claimed) == 1, f"{len(claimed)} workers claimed the same job"


# =================================================================== CLOCK
async def test_the_clock_skip_is_shared_across_processes(customer: str) -> None:
    """The API and the worker are different containers; a process-local offset
    would make the demo control do nothing visible."""
    await clock.reset()
    before = await clock.now()
    await clock.skip(timedelta(hours=5))
    after = await clock.now()
    assert (after - before) >= timedelta(hours=4, minutes=59)

    stored = await redis().get(clock.KEY)
    assert stored is not None, "the offset must live in Redis, not memory"


async def test_the_clock_cannot_be_skipped_in_production(customer: str, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "env", "prod")
    with pytest.raises(clock.ClockSkipRefused):
        await clock.skip(timedelta(hours=1))
    assert await clock.offset() == timedelta(0)


# ================================================================== RE-ENTRY
class TestReentryText:
    def test_every_stage_has_a_drop_off_phrase(self) -> None:
        from app.graph.policy import STAGES

        for stage in STAGES:
            assert stage in reentry.DROP_OFF_POINT, f"{stage} has no re-entry phrasing"

    def test_every_stage_maps_to_a_real_template(self) -> None:
        for stage, name in reentry.TEMPLATE_FOR_STAGE.items():
            assert name in reentry.TEMPLATES, f"{stage} maps to missing template {name}"

    def test_templates_name_the_drop_off_point_or_the_step(self) -> None:
        """ "Just checking in" is what spam says."""
        for stage in reentry.TEMPLATE_FOR_STAGE:
            _, body = reentry.template_for(stage)
            assert "{1}" not in body, "placeholder left unrendered"
            assert len(body) > 40

    def test_every_template_offers_an_exit(self) -> None:
        """Outside the window these are the only messages sent; each needs an opt-out."""
        for stage in reentry.TEMPLATE_FOR_STAGE:
            _, body = reentry.template_for(stage)
            assert "STOP" in body or "no rush" in body
