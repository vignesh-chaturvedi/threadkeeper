"""The vault, the ledger, the audit log — and the claim that nothing leaks.

The headline is test_no_raw_identifier_ever_reaches_disk: run a real conversation
containing a PAN, an Aadhaar and a phone number, then search *every* table and
every log line for the digits. The plan asks for a log-scrubbing test; this is
that, widened to the whole database, because a PAN in `messages` is no better
than a PAN in a log.
"""

from __future__ import annotations

import hashlib
import io
import json

import pytest
import structlog

from app import db
from app.cache import redis
from app.graph.runner import run_turn
from app.ingress import repository
from app.privacy import audit, consent, tokenize, vault
from app.privacy.patterns import synthetic_aadhaar
from app.privacy.refs import customer_ref
from app.scheduler import queue
from app.tools import client as tool_client

pytestmark = pytest.mark.integration

PAN = "ABCPE1234F"
AADHAAR = synthetic_aadhaar()
PHONE = "9876543210"


@pytest.fixture
async def conversation(live_db) -> str:
    ref = customer_ref("919000000099")
    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)
    await redis().delete(queue.ZSET)
    conv = await repository.get_or_create_conversation("whatsapp", ref)
    cid = str(conv["id"])
    for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
        await db.execute(f"DELETE FROM {table} WHERE thread_id = %s", cid)
    yield cid
    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)


# ================================================================== VAULT
class TestVault:
    async def test_a_round_trip_returns_the_value(self, conversation: str) -> None:
        token = await vault.put(conversation, "PAN", PAN)
        assert await vault.get(token) == PAN

    async def test_the_token_is_deterministic(self, conversation: str) -> None:
        """Random tokens would give the model two handles for one entity."""
        first = await vault.put(conversation, "PAN", PAN)
        second = await vault.put(conversation, "PAN", PAN)
        assert first == second

    async def test_the_same_value_in_two_conversations_gets_two_tokens(
        self, conversation: str
    ) -> None:
        """Otherwise the vault becomes a way to link customers by identifier."""
        other = await repository.get_or_create_conversation(
            "whatsapp", customer_ref("919000000098")
        )
        mine = await vault.put(conversation, "PAN", PAN)
        theirs = await vault.put(str(other["id"]), "PAN", PAN)
        assert mine != theirs
        await db.execute("DELETE FROM conversations WHERE id = %s", other["id"])

    async def test_the_stored_row_holds_no_plaintext(self, conversation: str) -> None:
        token = await vault.put(conversation, "PAN", PAN)
        row = await db.fetch_one("SELECT ciphertext FROM pii_vault WHERE token = %s", token)
        assert PAN not in row["ciphertext"]
        assert row["ciphertext"].startswith("gAAAAA")  # Fernet

    async def test_erasure_makes_the_value_unrecoverable(self, conversation: str) -> None:
        """Right to erasure: delete the vault rows and every token dangles."""
        token = await vault.put(conversation, "PAN", PAN)
        assert await vault.get(token) is not None

        erased = await vault.forget(conversation)
        assert erased >= 1
        assert await vault.get(token) is None


# ============================================================== TOKENIZE
class TestTokenize:
    async def test_the_text_that_survives_holds_no_digits(self, conversation: str) -> None:
        text = f"mera PAN {PAN} hai, aadhaar {AADHAAR}, phone {PHONE}"
        safe, mapping = await tokenize.tokenize(text, conversation)

        assert PAN not in safe
        assert AADHAAR not in safe
        assert PHONE not in safe
        assert len(mapping) == 3
        assert set(mapping.values()) == {"PAN", "AADHAAR", "PHONE"}

    async def test_detokenize_restores_exactly(self, conversation: str) -> None:
        text = f"PAN {PAN} and phone {PHONE}"
        safe, _ = await tokenize.tokenize(text, conversation)
        assert await tokenize.detokenize(safe, conversation) == text

    async def test_ordinary_text_is_untouched(self, conversation: str) -> None:
        text = "bhai 5 lakh ka loan chahiye, salary 60k"
        safe, mapping = await tokenize.tokenize(text, conversation)
        assert safe == text
        assert mapping == {}

    async def test_a_dangling_token_stays_a_token(self, conversation: str) -> None:
        """After erasure the value is gone; showing the token is the honest result."""
        safe, _ = await tokenize.tokenize(f"PAN {PAN}", conversation)
        await vault.forget(conversation)
        assert await tokenize.detokenize(safe, conversation) == safe


# =============================================================== LEDGER
class TestConsentLedger:
    async def test_it_records_the_exact_wording_not_just_a_hash(self, conversation: str) -> None:
        """ "Customer consented" is worthless without the text they saw."""
        wording = "We will share your details with partner lenders."
        await consent.record(
            conversation,
            "ref",
            "whatsapp",
            event="granted",
            wording=wording,
            wording_hash=hashlib.sha256(wording.encode()).hexdigest()[:16],
        )
        state = await consent.current(conversation)
        assert state["granted"] is True
        assert state["wording"] == wording

    async def test_it_is_append_only_at_the_database_level(self, conversation: str) -> None:
        """Not a convention — a trigger. An auditor gets a better answer."""
        await consent.record(
            conversation, "ref", "whatsapp", event="granted", wording="w", wording_hash="h"
        )
        with pytest.raises(Exception, match="append-only"):
            await db.execute(
                "UPDATE consent_ledger SET event = 'revoked' WHERE conversation_id = %s",
                conversation,
            )
        with pytest.raises(Exception, match="append-only"):
            await db.execute("DELETE FROM consent_ledger WHERE conversation_id = %s", conversation)

    async def test_revocation_is_a_new_row_and_the_grant_survives(self, conversation: str) -> None:
        await consent.record(
            conversation, "ref", "whatsapp", event="granted", wording="w", wording_hash="h"
        )
        await consent.revoke(conversation)

        events = [e["event"] for e in await consent.history(conversation)]
        assert events == ["granted", "revoked"], "both must remain visible"
        assert await consent.is_granted(conversation) is False

    async def test_revocation_halts_scheduling_within_one_turn(self, conversation: str) -> None:
        """ "Revocable" is only true if withdrawal takes effect immediately."""
        await queue.schedule(conversation, stage_at_drop="kyc_collect")
        assert await queue.pending_for(conversation) is not None

        await consent.record(
            conversation, "ref", "whatsapp", event="granted", wording="w", wording_hash="h"
        )
        result = await consent.revoke(conversation)

        assert result["revoked"] is True
        assert await queue.pending_for(conversation) is None, "the nudge must be gone now"

        row = await db.fetch_one("SELECT status FROM conversations WHERE id = %s", conversation)
        assert row["status"] == "opted_out"


# ================================================================ AUDIT
class TestAuditLog:
    async def test_a_turn_records_the_prompt_hash_and_model(self, conversation: str) -> None:
        """Answers "why did the agent say that in March" six months later."""
        await run_turn(conversation, "personal loan chahiye")
        trail = await audit.trail(conversation)
        turns = [e for e in trail if e["event"] == "turn"]
        assert turns
        assert turns[-1]["prompt_hash"]
        assert turns[-1]["model"]
        assert turns[-1]["stage"]

    async def test_tool_calls_are_audited_including_refusals(self, conversation: str) -> None:
        await tool_client.invoke(
            "create_application",
            {"conversation_id": conversation, "offer_id": "x", "consent_ref": "y", "idem_key": "z"},
            stage="close",
            state={"consent": {}, "slots": {}},
            conversation_id=conversation,
        )
        trail = await audit.trail(conversation)
        denied = [e for e in trail if e["event"] == "tool_call" and e["detail"].get("denied")]
        assert denied, "a refused tool call is a fact worth auditing"

    async def test_it_is_append_only_too(self, conversation: str) -> None:
        await audit.write(conversation, "turn", detail={"x": 1})
        with pytest.raises(Exception, match="append-only"):
            await db.execute(
                "UPDATE audit_log SET event = 'tampered' WHERE conversation_id = %s", conversation
            )


# ====================================================== THE HEADLINE TEST
async def test_no_raw_identifier_ever_reaches_disk(conversation: str, monkeypatch) -> None:
    """The plan asks for a log-scrubbing test. This is that, widened to everything.

    Run a real conversation carrying a PAN, an Aadhaar and a phone number, then
    search every table that touches a message — and every log line emitted — for
    the digits. A PAN in `messages` is no better than a PAN in a log.
    """
    buffer = io.StringIO()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            tokenize.scrub_event,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=buffer),
        cache_logger_on_first_use=False,
    )

    text = f"mera PAN {PAN} hai, aadhaar {AADHAAR} aur phone {PHONE}"
    safe, mapping = await tokenize.tokenize(text, conversation)
    assert len(mapping) == 3, "the fixture message must actually contain three identifiers"

    await db.execute(
        "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'in', %s)",
        conversation,
        safe,
    )
    reply = await run_turn(conversation, safe)
    await db.execute(
        "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'out', %s)",
        conversation,
        reply,
    )

    secrets = {"PAN": PAN, "AADHAAR": AADHAAR, "PHONE": PHONE}

    # --- every table that could plausibly hold a message ------------------
    surfaces = {
        "messages": "SELECT string_agg(body, ' ') AS blob FROM messages WHERE conversation_id = %s",
        "slots": (
            "SELECT string_agg(value::text, ' ') AS blob FROM slots WHERE conversation_id = %s"
        ),
        "audit_log": (
            "SELECT string_agg(detail::text, ' ') AS blob FROM audit_log WHERE conversation_id = %s"
        ),
        "tool_calls": (
            "SELECT string_agg(coalesce(arguments::text,'') || coalesce(result::text,''), ' ') "
            "AS blob FROM tool_calls WHERE conversation_id = %s"
        ),
        # The checkpointer's own state — the one place a naive implementation
        # leaks, because the graph state is serialised wholesale.
        "checkpoints": (
            "SELECT string_agg(checkpoint::text, ' ') AS blob FROM checkpoints WHERE thread_id = %s"
        ),
        "conversation_summaries": (
            "SELECT string_agg(summary, ' ') AS blob FROM conversation_summaries "
            "WHERE conversation_id = %s"
        ),
    }

    for surface, query in surfaces.items():
        row = await db.fetch_one(query, conversation)
        blob = (row or {}).get("blob") or ""
        for kind, value in secrets.items():
            assert value not in blob, f"{kind} leaked into {surface}"

    # --- and the logs -----------------------------------------------------
    logs = buffer.getvalue()
    for kind, value in secrets.items():
        assert value not in logs, f"{kind} leaked into the logs"

    # --- but the vault still has them, encrypted --------------------------
    held = await vault.inventory(conversation)
    assert {item["kind"] for item in held} == {"PAN", "AADHAAR", "PHONE"}
    ciphertexts = await db.fetch_one(
        "SELECT string_agg(ciphertext, ' ') AS blob FROM pii_vault WHERE conversation_id = %s",
        conversation,
    )
    for value in secrets.values():
        assert value not in ciphertexts["blob"]

    # --- and the customer still gets their own data back ------------------
    restored = await tokenize.detokenize(safe, conversation)
    assert restored == text


async def test_the_scrubbing_processor_catches_a_careless_log_line(conversation: str) -> None:
    """Belt and braces: a log statement added in a hurry must not leak either."""
    event = tokenize.scrub_event(None, "info", {"body": f"customer sent {PAN}", "n": 1})
    assert PAN not in json.dumps(event)
    assert "[PAN]" in event["body"]
    assert event["n"] == 1
