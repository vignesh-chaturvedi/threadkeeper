"""Tier 3 and the assembled context, against real Postgres and pgvector."""

from __future__ import annotations

import pytest

from app import db
from app.graph.runner import run_turn
from app.ingress import repository
from app.memory import semantic
from app.privacy.refs import customer_ref
from app.settings import get_settings

pytestmark = pytest.mark.integration


@pytest.fixture
async def customer(live_db):
    """One simulated customer, wiped before and after."""
    phone = "919000000077"
    ref = customer_ref(phone)
    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)
    yield ref
    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)


async def _conversation(ref: str) -> str:
    conv = await repository.get_or_create_conversation("whatsapp", ref)
    return str(conv["id"])


async def _say(cid: str, text: str) -> str:
    await db.execute(
        "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'in', %s)", cid, text
    )
    reply = await run_turn(cid, text)
    await db.execute(
        "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'out', %s)", cid, reply
    )
    return reply


# ========================================================== tier 3 mechanics
async def test_a_summary_is_stored_when_a_conversation_ends(customer: str) -> None:
    cid = await _conversation(customer)
    await _say(cid, "personal loan chahiye")
    await _say(cid, "interest rate bahut zyada hai")
    await _say(cid, "nahi chahiye, band karo")  # → close

    row = await db.fetch_one(
        "SELECT summary, objections, outcome, embedding IS NOT NULL AS has_vector "
        "FROM conversation_summaries WHERE conversation_id = %s",
        cid,
    )
    assert row is not None, "a closed conversation should leave a summary"
    assert row["summary"]
    assert row["has_vector"], "the summary should be embedded"
    assert row["outcome"] == "opted_out"


async def test_summaries_are_one_per_conversation_not_per_message(customer: str) -> None:
    """The design decision, asserted. Per-message embedding retrieves greetings."""
    cid = await _conversation(customer)
    for text in ["hi", "ok", "personal loan chahiye", "stop"]:
        await _say(cid, text)

    row = await db.fetch_one(
        "SELECT count(*) AS n FROM conversation_summaries WHERE conversation_id = %s", cid
    )
    assert row["n"] == 1, "four messages, one summary"


async def test_recall_finds_a_prior_conversation_for_the_same_customer(customer: str) -> None:
    first = await _conversation(customer)
    await _say(first, "personal loan chahiye")
    await _say(first, "processing fees bahut zyada hain, nahi chahiye")

    stored = await db.fetch_one(
        "SELECT count(*) AS n FROM conversation_summaries WHERE customer_ref = %s", customer
    )
    assert stored["n"] >= 1

    recalled = await semantic.recall(customer, "loan ke baare mein pooch raha hoon")
    assert recalled, "a returning customer's prior conversation should be recallable"
    assert recalled[0]["summary"]


async def test_recall_never_crosses_customers(customer: str) -> None:
    """A vector index that ignores the customer filter is a privacy incident."""
    cid = await _conversation(customer)
    await _say(cid, "personal loan chahiye")
    await _say(cid, "stop")

    other = customer_ref("919000000078")
    recalled = await semantic.recall(other, "personal loan")
    assert recalled == [], "another customer's history must not be retrievable"


async def test_recall_excludes_the_conversation_in_progress(customer: str) -> None:
    """Recalling the current conversation as 'history' would double-count it."""
    cid = await _conversation(customer)
    await _say(cid, "personal loan chahiye")
    await _say(cid, "stop")

    recalled = await semantic.recall(customer, "loan", exclude_conversation=cid)
    assert all(r["summary"] for r in recalled)
    row = await db.fetch_one(
        "SELECT summary FROM conversation_summaries WHERE conversation_id = %s", cid
    )
    assert row["summary"] not in [r["summary"] for r in recalled]


async def test_tier_3_can_be_switched_off(customer: str, monkeypatch) -> None:
    """evals/memory_ab.py depends on this being a real switch, not a no-op."""
    monkeypatch.setattr(get_settings(), "enable_semantic_memory", False)
    assert await semantic.recall(customer, "anything") == []


# ============================================================ assembled context
async def test_the_profile_reaches_the_prompt(customer: str, monkeypatch) -> None:
    """Tier 2 is only useful if it actually lands in the model call."""
    from app.llm import fake

    seen: list[str] = []
    real = fake.FakeProvider.reply

    async def spy(self, **kw):
        seen.append(kw.get("user", ""))
        return await real(self, **kw)

    cid = await _conversation(customer)
    await _say(cid, "personal loan chahiye, 5 lakh")

    monkeypatch.setattr(fake.FakeProvider, "reply", spy)
    await _say(cid, "haan theek hai")

    assert seen, "the reply call should have happened"
    prompt = seen[-1]
    assert "KNOWN ABOUT THIS CUSTOMER" in prompt
    assert "personal loan" in prompt
    assert "5 lakh" in prompt, "the amount should be rendered readably, not as 500000"


async def test_history_is_trimmed_to_the_token_budget(customer: str, monkeypatch) -> None:
    """Tier 1, end to end: a tiny budget must visibly drop old turns."""
    from app import memory

    cid = await _conversation(customer)
    for i in range(6):
        await db.execute(
            "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'in', %s)",
            cid,
            f"message number {i} " + "padding " * 40,
        )

    monkeypatch.setattr(get_settings(), "working_budget_tokens", 120)
    small = await memory.assemble(cid, customer, {}, {}, "now what")

    monkeypatch.setattr(get_settings(), "working_budget_tokens", 100_000)
    large = await memory.assemble(cid, customer, {}, {}, "now what")

    assert len(small.history) < len(large.history)
    assert small.history[-1] == large.history[-1], "both must keep the newest turn"


async def test_the_profile_is_reserved_before_history(customer: str, monkeypatch) -> None:
    """Priority statement: dense context should not be crowded out by transcript."""
    from app import memory

    cid = await _conversation(customer)
    for i in range(8):
        await db.execute(
            "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'in', %s)",
            cid,
            f"turn {i} " + "words " * 60,
        )

    monkeypatch.setattr(get_settings(), "working_budget_tokens", 200)
    got = await memory.assemble(
        cid, customer, {"product": "personal_loan", "income_band": "50k_1l"}, {}, "hi"
    )

    assert got.profile_block, "the profile must survive a tight budget"
    assert "profile" in got.tiers


async def test_a_returning_customer_is_flagged_only_on_the_opening_turn(customer: str) -> None:
    """The flag that made tier 3 worth its tokens.

    Measured in evals/memory_ab.py: with recall present but no instruction to
    use it, objection recall was 0/2 — the model correctly obeyed the stage
    guidance instead. Flagging the opening turn took it to 2/2.
    """
    from app import memory

    first = await _conversation(customer)
    await _say(first, "personal loan chahiye")
    await _say(first, "processing fees zyada hain, nahi chahiye")

    # A second conversation with the same customer, as a return visit would be.
    await db.execute(
        "UPDATE conversations SET customer_ref = %s WHERE id = %s", f"{customer}-old", first
    )
    await db.execute(
        "UPDATE conversation_summaries SET customer_ref = %s WHERE conversation_id = %s",
        customer,
        first,
    )
    second = await _conversation(customer)

    opening = await memory.assemble(second, customer, {}, {}, "wapas aaya hoon")
    assert opening.recall_block, "their prior conversation should be recalled"
    assert opening.returning is True, "the opening turn must be flagged"

    # Mid-conversation, the same recall must NOT be flagged as an opening.
    await db.execute(
        "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'in', %s)",
        second,
        "aur batao",
    )
    await db.execute(
        "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'out', %s)",
        second,
        "ji boliye",
    )
    later = await memory.assemble(second, customer, {}, {}, "aur batao")
    assert later.returning is False, "bringing it up mid-conversation would be jarring"

    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", f"{customer}-old")
