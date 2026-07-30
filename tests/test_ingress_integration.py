"""Phase 01 acceptance, against a real Postgres.

The headline is test_five_deliveries_store_one_message_and_send_one_reply — the
plan's done-when, written as an assertion rather than a claim in a README.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest

from app import db
from app.ingress import repository
from app.ingress.events import OutboundMessage
from app.privacy.refs import customer_ref
from app.settings import get_settings

pytestmark = pytest.mark.integration

SECRET = "test-app-secret"


def _body_and_headers(text: str, msg_id: str, sender: str) -> tuple[bytes, dict[str, str]]:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "messages": [
                                {
                                    "from": sender,
                                    "id": msg_id,
                                    "timestamp": "1690000000",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ]
            }
        ],
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    mac = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, {"x-hub-signature-256": f"sha256={mac}", "content-type": "application/json"}


async def _counts(phone: str) -> tuple[int, int]:
    """(inbound stored, outbound sent) for one customer."""
    row = await db.fetch_one(
        """
        SELECT
          count(*) FILTER (WHERE m.direction = 'in')  AS inbound,
          count(*) FILTER (WHERE m.direction = 'out') AS outbound
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE c.customer_ref = %s
        """,
        customer_ref(phone),
    )
    return (row["inbound"], row["outbound"]) if row else (0, 0)


# ============================================================== THE DONE-WHEN
async def test_five_deliveries_store_one_message_and_send_one_reply(
    live_app, clean_conversation: str
) -> None:
    """Post the identical payload five times. The provider does this for real."""
    phone = clean_conversation
    body, headers = _body_and_headers("hello there", "wamid.dedupe.1", phone)

    results = [
        await live_app.post("/webhook/whatsapp", content=body, headers=headers) for _ in range(5)
    ]

    assert [r.status_code for r in results] == [200] * 5
    assert results[0].json() == {"ok": True, "accepted": 1, "duplicates": 0}
    for r in results[1:]:
        assert r.json() == {"ok": True, "accepted": 0, "duplicates": 1}

    await asyncio.sleep(1.2)  # debounce window (0.3s in tests) + send

    inbound, outbound = await _counts(phone)
    assert inbound == 1, "five deliveries must store exactly one message"
    assert outbound == 1, "five deliveries must produce exactly one reply"


async def test_concurrent_redelivery_still_dedupes(live_app, clean_conversation: str) -> None:
    """The race a SELECT-then-INSERT would lose. The unique index cannot."""
    phone = clean_conversation
    body, headers = _body_and_headers("concurrent", "wamid.race.1", phone)

    responses = await asyncio.gather(
        *[live_app.post("/webhook/whatsapp", content=body, headers=headers) for _ in range(8)]
    )
    assert all(r.status_code == 200 for r in responses)
    assert sum(r.json()["accepted"] for r in responses) == 1

    await asyncio.sleep(1.2)
    inbound, outbound = await _counts(phone)
    assert (inbound, outbound) == (1, 1)


# ==================================================================== signature
async def test_an_unsigned_request_is_rejected_and_stores_nothing(
    live_app, clean_conversation: str
) -> None:
    phone = clean_conversation
    body, _ = _body_and_headers("should not land", "wamid.unsigned", phone)

    r = await live_app.post(
        "/webhook/whatsapp", content=body, headers={"content-type": "application/json"}
    )
    assert r.status_code == 401
    assert await _counts(phone) == (0, 0)


async def test_a_tampered_body_is_rejected(live_app, clean_conversation: str) -> None:
    phone = clean_conversation
    _, headers = _body_and_headers("original", "wamid.tamper", phone)
    other, _ = _body_and_headers("tampered", "wamid.tamper", phone)

    r = await live_app.post("/webhook/whatsapp", content=other, headers=headers)
    assert r.status_code == 401
    assert await _counts(phone) == (0, 0)


# ===================================================================== behaviour
async def test_distinct_messages_each_get_a_reply(live_app, clean_conversation: str) -> None:
    """Separate thoughts still get separate replies.

    Phase 02 changed what this test has to say. Sent back-to-back these three
    messages are now deliberately coalesced into one turn, so "distinct" has to
    mean genuinely distinct — spaced beyond the debounce window.
    """
    phone = clean_conversation
    for i in range(3):
        body, headers = _body_and_headers(f"message {i}", f"wamid.distinct.{i}", phone)
        r = await live_app.post("/webhook/whatsapp", content=body, headers=headers)
        assert r.json()["accepted"] == 1
        await asyncio.sleep(0.9)  # > buffer_window_s, so each is its own turn

    await asyncio.sleep(0.8)
    assert await _counts(phone) == (3, 3)


async def test_one_conversation_per_customer(live_app, clean_conversation: str) -> None:
    """Two messages, one conversation row — the upsert must not create a second."""
    phone = clean_conversation
    for i in range(2):
        body, headers = _body_and_headers("hi", f"wamid.conv.{i}", phone)
        await live_app.post("/webhook/whatsapp", content=body, headers=headers)

    row = await db.fetch_one(
        "SELECT count(*) AS n FROM conversations WHERE customer_ref = %s", customer_ref(phone)
    )
    assert row["n"] == 1


async def test_last_in_at_is_stamped(live_app, clean_conversation: str) -> None:
    """Phase 06 reads this to decide template-vs-freeform for the 24h window."""
    phone = clean_conversation
    body, headers = _body_and_headers("hi", "wamid.stamp", phone)
    await live_app.post("/webhook/whatsapp", content=body, headers=headers)
    await asyncio.sleep(0.6)  # stamped by the buffer's push, not the webhook

    row = await db.fetch_one(
        "SELECT last_in_at FROM conversations WHERE customer_ref = %s", customer_ref(phone)
    )
    assert row["last_in_at"] is not None


async def test_status_callback_is_accepted_but_does_nothing(
    live_app, clean_conversation: str
) -> None:
    payload = {"entry": [{"changes": [{"value": {"statuses": [{"status": "read"}]}}]}]}
    body = json.dumps(payload, separators=(",", ":")).encode()
    mac = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

    r = await live_app.post(
        "/webhook/whatsapp",
        content=body,
        headers={"x-hub-signature-256": f"sha256={mac}", "content-type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["accepted"] == 0


async def test_malformed_json_is_400_not_500(live_app) -> None:
    """A 5xx makes the provider retry a body we can never parse."""
    body = b"{not json"
    mac = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    r = await live_app.post(
        "/webhook/whatsapp", content=body, headers={"x-hub-signature-256": f"sha256={mac}"}
    )
    assert r.status_code == 400


# ====================================================================== outbound
async def test_exhausted_retries_become_a_dead_letter(live_db, monkeypatch) -> None:
    """A failed send must be visible in a table, never silently lost."""
    from app.ingress import outbound
    from app.ingress.adapters import TransientSendError, get_adapter

    phone = "919000000009"
    ref = customer_ref(phone)
    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)
    conv = await repository.get_or_create_conversation("whatsapp", ref)
    conversation_id = str(conv["id"])

    attempts = {"n": 0}

    async def always_fails(_message):  # type: ignore[no-untyped-def]
        attempts["n"] += 1
        raise TransientSendError("provider 503")

    monkeypatch.setattr(get_adapter("whatsapp"), "send", always_fails)
    monkeypatch.setattr(get_settings(), "outbound_backoff_s", (0.0, 0.0, 0.0))

    result = await outbound.send(
        OutboundMessage(
            channel="whatsapp",
            conversation_id=conversation_id,
            customer_ref=ref,
            text="never delivered",
        )
    )

    assert attempts["n"] == get_settings().outbound_max_attempts
    assert result.provider_msg_id is None

    row = await db.fetch_one(
        "SELECT body, attempts, last_error FROM outbound_dead_letters WHERE conversation_id = %s",
        conversation_id,
    )
    assert row is not None
    assert row["body"] == "never delivered"
    assert row["attempts"] == get_settings().outbound_max_attempts
    assert "503" in row["last_error"]

    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)


async def test_a_permanent_failure_is_not_retried(live_db, monkeypatch) -> None:
    """Retrying an unapproved template or a revoked token just wastes quota."""
    from app.ingress import outbound
    from app.ingress.adapters import PermanentSendError, get_adapter

    phone = "919000000010"
    ref = customer_ref(phone)
    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)
    conv = await repository.get_or_create_conversation("whatsapp", ref)

    attempts = {"n": 0}

    async def rejected(_message):  # type: ignore[no-untyped-def]
        attempts["n"] += 1
        raise PermanentSendError("provider 400: invalid recipient")

    monkeypatch.setattr(get_adapter("whatsapp"), "send", rejected)

    await outbound.send(
        OutboundMessage(
            channel="whatsapp",
            conversation_id=str(conv["id"]),
            customer_ref=ref,
            text="bad recipient",
        )
    )

    assert attempts["n"] == 1, "permanent failures must not be retried"
    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)


async def test_a_transient_failure_recovers_on_retry(live_db, monkeypatch) -> None:
    from app.ingress import outbound
    from app.ingress.adapters import TransientSendError, get_adapter

    phone = "919000000011"
    ref = customer_ref(phone)
    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)
    conv = await repository.get_or_create_conversation("whatsapp", ref)

    attempts = {"n": 0}

    async def flaky(_message):  # type: ignore[no-untyped-def]
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TransientSendError("timeout")
        return "wamid.recovered"

    monkeypatch.setattr(get_adapter("whatsapp"), "send", flaky)
    monkeypatch.setattr(get_settings(), "outbound_backoff_s", (0.0, 0.0, 0.0))

    result = await outbound.send(
        OutboundMessage(
            channel="whatsapp",
            conversation_id=str(conv["id"]),
            customer_ref=ref,
            text="eventually delivered",
        )
    )

    assert result.provider_msg_id == "wamid.recovered"
    assert result.attempts == 3

    row = await db.fetch_one(
        "SELECT count(*) AS n FROM outbound_dead_letters WHERE conversation_id = %s", conv["id"]
    )
    assert row["n"] == 0, "a recovered send must not leave a dead letter"

    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)


# ===================================================================== simulator
@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("ascii", "hello there"),
        ("hinglish", "PAN nahi hai abhi"),
        ("devanagari", "ब्याज दर क्या है"),
    ],
)
async def test_simulator_signature_survives_non_ascii(live_app, label: str, text: str) -> None:
    """The simulator signs, the browser serialises. Both must produce the same bytes.

    Python's json.dumps escapes non-ASCII by default and JavaScript's
    JSON.stringify does not, so without ensure_ascii=False every Devanagari
    message would fail signature verification with a 401 — and it would look
    like a signing bug rather than an encoding one.
    """
    built = (
        await live_app.post("/sim/api/payload", json={"phone": "919555000222", "text": text})
    ).json()

    # Exactly what JSON.stringify emits.
    body = json.dumps(built["payload"], separators=(",", ":"), ensure_ascii=False).encode()

    r = await live_app.post(
        "/webhook/whatsapp",
        content=body,
        headers={
            "content-type": "application/json",
            "x-hub-signature-256": built["signature"],
        },
    )
    assert r.status_code == 200, f"{label} payload failed signature verification"
    assert r.json()["accepted"] == 1
