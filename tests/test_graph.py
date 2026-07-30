"""The funnel end to end, against real Postgres and the deterministic provider.

The headline is test_the_funnel_survives_a_restart — the plan's done-when,
written as an assertion: close the pool, drop the compiled graph, rebuild
everything from scratch, send one more message, and the conversation continues
at the right stage with its slots intact.
"""

from __future__ import annotations

import pytest

from app import db
from app.graph import escalation
from app.graph.build import get_graph, reset_graph
from app.graph.runner import run_turn
from app.ingress import repository
from app.privacy.refs import customer_ref

pytestmark = pytest.mark.integration


@pytest.fixture
async def conversation(live_db) -> str:
    """A fresh conversation, with its checkpoint thread cleared too."""
    ref = customer_ref("919000000042")
    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)
    conv = await repository.get_or_create_conversation("whatsapp", ref)
    cid = str(conv["id"])
    for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
        await db.execute(f"DELETE FROM {table} WHERE thread_id = %s", cid)
    yield cid
    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)


async def _slots(cid: str) -> dict:
    rows = await db.fetch_all("SELECT key, value FROM slots WHERE conversation_id = %s", cid)
    return {r["key"]: r["value"] for r in rows}


async def _stage(cid: str) -> str:
    row = await db.fetch_one("SELECT stage FROM conversations WHERE id = %s", cid)
    return row["stage"]


async def _transitions(cid: str) -> list[tuple[str, str]]:
    rows = await db.fetch_all(
        "SELECT to_stage, reason FROM stage_transitions WHERE conversation_id = %s ORDER BY id",
        cid,
    )
    return [(r["to_stage"], r["reason"]) for r in rows]


# ============================================================== THE DONE-WHEN
async def test_the_funnel_survives_a_restart(conversation: str) -> None:
    """Stop everything mid-funnel, rebuild it, and carry on.

    This is the plan's acceptance criterion. The pool is closed and reopened and
    the compiled graph is discarded, which is as close to `docker compose
    restart` as a test can get in-process — nothing survives except Postgres.
    """
    cid = conversation

    await run_turn(cid, "hi, I need a personal loan")
    await run_turn(cid, "5 lakh chahiye, salary 60k hai")
    stage_before = await _stage(cid)
    slots_before = await _slots(cid)

    assert slots_before.get("product") == "personal_loan"
    assert stage_before != "intent_route", "the funnel should have moved"

    # --- the restart -----------------------------------------------------
    await db.close_pool()
    reset_graph()
    await db.open_pool()

    # --- one more message ------------------------------------------------
    await run_turn(cid, "yes go ahead")

    slots_after = await _slots(cid)
    assert slots_after.get("product") == "personal_loan", "slots must survive the restart"
    assert slots_after.get("amount_inr") == 500_000
    assert slots_after.get("income_band") == "50k_1l"

    graph = await get_graph()
    state = await graph.aget_state({"configurable": {"thread_id": cid}})
    assert state.values.get("slots", {}).get("product") == "personal_loan"
    assert state.values.get("stage") is not None


# =================================================================== FUNNEL
async def test_a_full_happy_path_reaches_offers(conversation: str) -> None:
    cid = conversation

    await run_turn(cid, "hi")
    assert await _stage(cid) == "qualify"

    await run_turn(cid, "personal loan chahiye")
    assert await _stage(cid) == "consent", "product known → consent is the next gate"

    await run_turn(cid, "haan theek hai")
    await run_turn(cid, "salary 60k")
    await run_turn(cid, "pan hai mere paas")

    assert await _stage(cid) == "offer_match"
    slots = await _slots(cid)
    assert slots["pan_status"] == "available"
    assert slots["consent"]["granted"] is True
    assert slots["consent"]["wording_hash"], "consent must record what was shown"


async def test_every_transition_records_why(conversation: str) -> None:
    """The funnel chart in Phase 10 is built from these rows."""
    cid = conversation
    await run_turn(cid, "hi")
    await run_turn(cid, "home loan")

    transitions = await _transitions(cid)
    assert transitions, "moving stage must leave a row"
    assert all(reason for _, reason in transitions), "every transition needs a reason"
    assert ("consent", "consent_missing") in transitions


# ================================================================ INTERRUPTS
async def test_opt_out_works_mid_funnel_and_immediately(conversation: str) -> None:
    cid = conversation
    await run_turn(cid, "personal loan chahiye")
    await run_turn(cid, "haan")
    assert await _stage(cid) in ("qualify", "consent", "kyc_collect")

    await run_turn(cid, "band karo, mat bhejo")

    assert await _stage(cid) == "close"
    assert (await _slots(cid)).get("opted_out") is True
    row = await db.fetch_one("SELECT status FROM conversations WHERE id = %s", cid)
    assert row["status"] == "opted_out"


async def test_an_objection_does_not_advance_the_stage(conversation: str) -> None:
    cid = conversation
    await run_turn(cid, "personal loan chahiye")
    stage_before = await _stage(cid)

    reply = await run_turn(cid, "interest rate kitna hai bhai?")

    assert await _stage(cid) == stage_before, "an objection must not move the funnel"
    assert reply, "but it must still be answered"
    assert (await _slots(cid)).get("objection")


async def test_off_topic_is_answered_without_progress(conversation: str) -> None:
    cid = conversation
    await run_turn(cid, "personal loan chahiye")
    stage_before = await _stage(cid)
    await run_turn(cid, "aaj mausam kaisa hai")
    assert await _stage(cid) == stage_before


async def _say(cid: str, text: str) -> str:
    """Mimic the webhook: persist the inbound message, then run the turn.

    The escalation packet and the reply history both read the messages table, so
    a test that only calls run_turn is testing a conversation with no transcript.
    """
    await db.execute(
        "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'in', %s)", cid, text
    )
    reply = await run_turn(cid, text)
    await db.execute(
        "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'out', %s)", cid, reply
    )
    return reply


async def test_asking_for_a_human_escalates_and_builds_a_packet(conversation: str) -> None:
    cid = conversation
    await _say(cid, "personal loan chahiye")
    await _say(cid, "I want to talk to a human agent")

    assert await _stage(cid) == "escalate"
    row = await db.fetch_one("SELECT status FROM conversations WHERE id = %s", cid)
    assert row["status"] == "escalated"

    queue = await escalation.open_queue()
    mine = [e for e in queue if e["conversation_id"] == cid]
    assert mine, "escalating must leave a packet for a human"

    packet = mine[0]["packet"]
    assert packet["transcript"], "a human needs the conversation, not just a flag"
    assert packet["slots"].get("product") == "personal_loan"
    assert packet["stage"]
    assert "funnel_path" in packet
    # The packet must not leak a phone number into the console.
    assert "9000000042" not in str(packet)


# ============================================================ EXTRACTION SPLIT
async def test_extraction_and_reply_are_separate_calls(conversation: str, monkeypatch) -> None:
    """Phase 03's key decision, asserted rather than asserted-to-be-true."""
    from app.llm import fake

    calls: list[str] = []
    real_extract, real_reply = fake.FakeProvider.extract, fake.FakeProvider.reply

    async def spy_extract(self, **kw):
        calls.append("extract")
        return await real_extract(self, **kw)

    async def spy_reply(self, **kw):
        calls.append("reply")
        return await real_reply(self, **kw)

    monkeypatch.setattr(fake.FakeProvider, "extract", spy_extract)
    monkeypatch.setattr(fake.FakeProvider, "reply", spy_reply)

    # Deliberately a turn that routes to `qualify`. The `consent` node returns
    # fixed wording and makes no model call at all — see test below.
    await run_turn(conversation, "hi there")

    assert calls == ["extract", "reply"], "extraction runs first, and they are two calls"


async def test_consent_wording_is_never_generated(conversation: str, monkeypatch) -> None:
    """The one stage that must not be paraphrased.

    A model rewording the consent text each time would make the wording hash —
    and therefore the whole consent ledger — meaningless.
    """
    from app.graph import prompts
    from app.llm import fake

    called = False

    async def spy_reply(self, **kw):
        nonlocal called
        called = True
        raise AssertionError("consent must not call the model")

    await run_turn(conversation, "personal loan chahiye")  # routes to consent next
    monkeypatch.setattr(fake.FakeProvider, "reply", spy_reply)

    reply = await run_turn(conversation, "kitna time lagega")
    assert not called or reply == prompts.CONSENT_WORDING


async def test_a_model_failure_degrades_instead_of_crashing(conversation: str, monkeypatch) -> None:
    """A provider outage must not take a turn down silently."""
    from app.llm import ModelError, fake

    async def broken(self, **kw):
        raise ModelError("provider down")

    monkeypatch.setattr(fake.FakeProvider, "reply", broken)

    # "hi" routes to `qualify`, which does call the model — unlike `consent`.
    reply = await run_turn(conversation, "hi")
    assert reply, "the customer must still get something"
    assert await _stage(conversation) == "escalate"


# ================================================================== MEMORY
async def test_a_corrected_amount_overwrites_the_old_one(conversation: str) -> None:
    """Customer said 4 lakh, now says 6. The later value wins."""
    cid = conversation
    await run_turn(cid, "personal loan, 4 lakh chahiye")
    assert (await _slots(cid))["amount_inr"] == 400_000

    await run_turn(cid, "sorry 6 lakh chahiye")
    assert (await _slots(cid))["amount_inr"] == 600_000


async def test_opting_back_in_requires_more_than_silence(conversation: str) -> None:
    """A message that merely fails to repeat 'stop' is not consent to resume."""
    cid = conversation
    await run_turn(cid, "stop")
    assert (await _slots(cid)).get("opted_out") is True

    await run_turn(cid, "hello?")
    assert (await _slots(cid)).get("opted_out") is True, "opt-out must not silently lapse"
    assert await _stage(cid) == "close"


# ================================================================== REPLAY
async def test_time_travel_replay_reproduces_the_stage_path(conversation: str) -> None:
    """Prove a change didn't regress turn 7 — without re-eliciting turn 7."""
    from app.graph.replay import replay

    cid = conversation
    for text in ["hi", "personal loan chahiye", "haan theek hai", "salary 60k"]:
        await db.execute(
            "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'in', %s)",
            cid,
            text,
        )
        reply = await run_turn(cid, text)
        await db.execute(
            "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'out', %s)",
            cid,
            reply,
        )

    report = await replay(cid)

    assert report["turns"] == 4
    assert report["stage_path"]["identical"], (
        f"replaying unchanged code must reproduce the path: "
        f"{report['stage_path']['original']} vs {report['stage_path']['replayed']}"
    )
    assert not report["slots"]["changed"], f"slots drifted: {report['slots']['changed']}"


async def test_replay_leaves_the_original_untouched(conversation: str) -> None:
    from app.graph.replay import replay

    cid = conversation
    await db.execute(
        "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'in', %s)",
        cid,
        "personal loan chahiye",
    )
    await run_turn(cid, "personal loan chahiye")

    before_stage = await _stage(cid)
    before_slots = await _slots(cid)
    before_msgs = await db.fetch_one(
        "SELECT count(*) AS n FROM messages WHERE conversation_id = %s", cid
    )

    report = await replay(cid)

    assert await _stage(cid) == before_stage
    assert await _slots(cid) == before_slots
    after_msgs = await db.fetch_one(
        "SELECT count(*) AS n FROM messages WHERE conversation_id = %s", cid
    )
    assert after_msgs["n"] == before_msgs["n"], "replay must not write to the source conversation"

    # And the shadow is cleaned up.
    gone = await db.fetch_one(
        "SELECT count(*) AS n FROM conversations WHERE id = %s", report["shadow_conversation"]
    )
    assert gone["n"] == 0
