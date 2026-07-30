"""Tool invocation, idempotency and the audit trail, against real Postgres."""

from __future__ import annotations

import asyncio

import pytest

from app import db
from app.graph.runner import run_turn
from app.ingress import repository
from app.privacy.refs import customer_ref
from app.settings import get_settings
from app.tools import client, registry

pytestmark = pytest.mark.integration


@pytest.fixture
async def conversation(live_db) -> str:
    ref = customer_ref("919000000055")
    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)
    conv = await repository.get_or_create_conversation("whatsapp", ref)
    cid = str(conv["id"])
    for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
        await db.execute(f"DELETE FROM {table} WHERE thread_id = %s", cid)
    yield cid
    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)


async def _grant_consent(cid: str) -> str:
    """Put a consent row on file, as the consent node would."""
    from app.graph import prompts

    wording_hash = prompts.consent_wording_hash()
    await db.execute(
        """
        INSERT INTO slots (conversation_id, key, value, source)
        VALUES (%s, 'consent', %s::jsonb, 'confirmed')
        ON CONFLICT (conversation_id, key) DO UPDATE SET value = EXCLUDED.value
        """,
        cid,
        f'{{"granted": true, "wording_hash": "{wording_hash}"}}',
    )
    return wording_hash


async def _fetch_offers(cid: str) -> list[dict]:
    result = await client.invoke(
        "fetch_offers",
        {
            "product": "personal_loan",
            "income_band": "above_1l",
            "city_tier": 1,
            "amount_inr": 500_000,
            "conversation_id": cid,
        },
        stage="offer_match",
        state={"consent": {"granted": True}, "slots": {}},
        conversation_id=cid,
    )
    return result.get("offers", [])


# ============================================================== the guard, live
async def test_a_denied_call_is_recorded_not_silently_dropped(conversation: str) -> None:
    """A refusal is a fact about what the agent tried. Auditors want it."""
    result = await client.invoke(
        "create_application",
        {"conversation_id": conversation, "offer_id": "x", "consent_ref": "y", "idem_key": "z"},
        stage="close",
        state={"consent": {}, "slots": {}},
        conversation_id=conversation,
    )
    assert result["error"] == "tool_not_permitted"
    assert result["reason"] == "consent_missing"

    row = await db.fetch_one(
        "SELECT denied_reason FROM tool_calls WHERE conversation_id = %s ORDER BY id DESC LIMIT 1",
        conversation,
    )
    assert row["denied_reason"] == "consent_missing"


async def test_the_handler_refuses_even_if_the_guard_is_bypassed(conversation: str) -> None:
    """Defence in depth: the guard knows graph state, the handler knows the database."""
    result = await registry.create_application(
        conversation_id=conversation, offer_id="off_x", consent_ref="whatever", idem_key="k1"
    )
    assert result["error"] == "consent_missing"


async def test_an_offer_that_was_never_quoted_is_rejected(conversation: str) -> None:
    """An offer id the customer was never shown is, by definition, invented."""
    ref = await _grant_consent(conversation)
    result = await registry.create_application(
        conversation_id=conversation,
        offer_id="off_totally_made_up",
        consent_ref=ref,
        idem_key="k2",
    )
    assert result["error"] == "offer_not_quoted"


async def test_a_mismatched_consent_ref_is_rejected(conversation: str) -> None:
    """Consent for different wording is not consent for this."""
    await _grant_consent(conversation)
    offers = await _fetch_offers(conversation)
    result = await registry.create_application(
        conversation_id=conversation,
        offer_id=offers[0]["offer_id"],
        consent_ref="hash_of_some_other_wording",
        idem_key="k3",
    )
    assert result["error"] == "consent_ref_mismatch"


# ================================================================ idempotency
async def test_a_retried_application_does_not_open_a_second(conversation: str) -> None:
    """The plan's requirement, stated as a test."""
    ref = await _grant_consent(conversation)
    await db.execute(
        """
        INSERT INTO slots (conversation_id, key, value, source)
        VALUES (%s, 'pan_status', '"available"'::jsonb, 'confirmed')
        ON CONFLICT (conversation_id, key) DO UPDATE SET value = EXCLUDED.value
        """,
        conversation,
    )
    offers = await _fetch_offers(conversation)
    state = {"consent": {"granted": True}, "slots": {"pan_status": "available"}}
    args = {
        "conversation_id": conversation,
        "offer_id": offers[0]["offer_id"],
        "consent_ref": ref,
    }

    first = await client.invoke(
        "create_application", dict(args), stage="close", state=state, conversation_id=conversation
    )
    second = await client.invoke(
        "create_application", dict(args), stage="close", state=state, conversation_id=conversation
    )

    assert first["application_id"] == second["application_id"]
    assert second.get("idempotent_replay") is True

    row = await db.fetch_one(
        "SELECT count(*) AS n FROM applications WHERE conversation_id = %s", conversation
    )
    assert row["n"] == 1, "a retry must not open a second loan application"


async def test_concurrent_retries_still_open_one(conversation: str) -> None:
    """The race a check-then-write loses — under exactly the conditions retries create."""
    ref = await _grant_consent(conversation)
    await db.execute(
        """
        INSERT INTO slots (conversation_id, key, value, source)
        VALUES (%s, 'pan_status', '"available"'::jsonb, 'confirmed')
        ON CONFLICT (conversation_id, key) DO UPDATE SET value = EXCLUDED.value
        """,
        conversation,
    )
    offers = await _fetch_offers(conversation)
    state = {"consent": {"granted": True}, "slots": {"pan_status": "available"}}
    args = {
        "conversation_id": conversation,
        "offer_id": offers[0]["offer_id"],
        "consent_ref": ref,
    }

    results = await asyncio.gather(
        *[
            client.invoke(
                "create_application",
                dict(args),
                stage="close",
                state=state,
                conversation_id=conversation,
            )
            for _ in range(6)
        ]
    )
    ids = {r.get("application_id") for r in results}
    assert len(ids) == 1, f"six concurrent retries produced {len(ids)} applications"

    row = await db.fetch_one(
        "SELECT count(*) AS n FROM applications WHERE conversation_id = %s", conversation
    )
    assert row["n"] == 1


async def test_the_idempotency_key_is_derived_from_intent_not_randomness(
    conversation: str,
) -> None:
    a = client.derive_idem_key(conversation, "create_application", {"offer_id": "off_1"})
    b = client.derive_idem_key(conversation, "create_application", {"offer_id": "off_1"})
    c = client.derive_idem_key(conversation, "create_application", {"offer_id": "off_2"})
    assert a == b, "the same intent must collide"
    assert a != c, "a different offer is a different intent"


# ============================================================ privacy + audit
async def test_a_pan_never_reaches_the_audit_log(conversation: str) -> None:
    """The tool needs the number; the record must not keep it."""
    await client.invoke(
        "verify_pan",
        {"pan": "ABCDE1234F", "conversation_id": conversation},
        stage="kyc_collect",
        state={"slots": {}, "consent": {}},
        conversation_id=conversation,
    )
    row = await db.fetch_one(
        "SELECT arguments, result FROM tool_calls WHERE conversation_id = %s "
        "AND tool = 'verify_pan' ORDER BY id DESC LIMIT 1",
        conversation,
    )
    assert "ABCDE1234F" not in str(row["arguments"]), "the PAN leaked into the audit log"
    assert row["arguments"]["pan"] == "***redacted***"
    assert row["result"]["verified"] is True
    assert "ABCDE1234F" not in str(row["result"])


async def test_every_call_is_recorded_with_latency(conversation: str) -> None:
    await _grant_consent(conversation)
    await _fetch_offers(conversation)
    row = await db.fetch_one(
        "SELECT tool, stage_at_call, latency_ms FROM tool_calls "
        "WHERE conversation_id = %s ORDER BY id DESC LIMIT 1",
        conversation,
    )
    assert row["tool"] == "fetch_offers"
    assert row["stage_at_call"] == "offer_match"
    assert row["latency_ms"] is not None


# =========================================================== degradation
async def test_a_lender_outage_degrades_instead_of_inventing(
    conversation: str, monkeypatch
) -> None:
    """The reason fault injection exists."""
    await _grant_consent(conversation)
    monkeypatch.setattr(get_settings(), "lender_failure_rate", 1.0)

    result = await client.invoke(
        "fetch_offers",
        {
            "product": "personal_loan",
            "income_band": "above_1l",
            "city_tier": 1,
            "amount_inr": 500_000,
            "conversation_id": conversation,
        },
        stage="offer_match",
        state={"consent": {"granted": True}, "slots": {}},
        conversation_id=conversation,
    )
    assert result["error"] in ("lender_timeout", "lender_unavailable")
    assert result["retryable"] is True
    assert "offers" not in result


async def test_the_funnel_reaches_offers_and_quotes_only_real_numbers(
    conversation: str, monkeypatch
) -> None:
    """End to end: the offer_match node calls the lender before it speaks."""
    monkeypatch.setattr(get_settings(), "lender_failure_rate", 0.0)

    for text in [
        "personal loan chahiye 5 lakh",
        "haan theek hai",
        "salary 1 lakh se zyada hai",
        "pan hai mere paas",
    ]:
        await db.execute(
            "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'in', %s)",
            conversation,
            text,
        )
        reply = await run_turn(conversation, text)
        await db.execute(
            "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'out', %s)",
            conversation,
            reply,
        )

    row = await db.fetch_one("SELECT stage FROM conversations WHERE id = %s", conversation)
    assert row["stage"] == "offer_match"

    call = await db.fetch_one(
        "SELECT result FROM tool_calls WHERE conversation_id = %s AND tool = 'fetch_offers' "
        "ORDER BY id DESC LIMIT 1",
        conversation,
    )
    assert call is not None, "reaching offer_match must have called the lender"
    assert call["result"]["offers"], "and the lender returned offers"


async def test_the_audit_trail_reads_back(conversation: str) -> None:
    await _grant_consent(conversation)
    await _fetch_offers(conversation)
    trail = await client.calls_for(conversation)
    assert trail
    assert trail[-1]["tool"] == "fetch_offers"
    assert trail[-1]["ok"] is True
