"""Phase 02 acceptance: a burst becomes one turn, and stale work never ships.

The headline is test_four_messages_300ms_apart_produce_exactly_one_reply — the
plan calls it demo gold, and it is the test worth screenshotting.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest

from app import db
from app.buffer import coalesce, lock
from app.cache import redis
from app.privacy.refs import customer_ref
from app.settings import get_settings

pytestmark = pytest.mark.integration

SECRET = "test-app-secret"


def _signed(text: str, msg_id: str, sender: str) -> tuple[bytes, dict[str, str]]:
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
    row = await db.fetch_one(
        """
        SELECT count(*) FILTER (WHERE m.direction = 'in')  AS inbound,
               count(*) FILTER (WHERE m.direction = 'out') AS outbound
        FROM messages m JOIN conversations c ON c.id = m.conversation_id
        WHERE c.customer_ref = %s
        """,
        customer_ref(phone),
    )
    return (row["inbound"], row["outbound"]) if row else (0, 0)


# ================================================================ DEMO GOLD
async def test_four_messages_300ms_apart_produce_exactly_one_reply(
    live_app, clean_conversation: str, monkeypatch
) -> None:
    """The plan's headline test.

    "hi" / "need loan" / "5 lakh" / "urgent bro" — four messages, six seconds,
    one human intent. Four replies would read as a broken bot.

    Deliberately run at the *shipping* window (2.5s / 8s) rather than the
    shrunken one the rest of the suite uses. It costs a few seconds and it means
    this asserts the configuration that actually runs in production.
    """
    s = get_settings()
    monkeypatch.setattr(s, "buffer_window_s", 2.5)
    monkeypatch.setattr(s, "buffer_max_hold_s", 8.0)

    phone = clean_conversation
    parts = ["hi", "need loan", "5 lakh", "urgent bro"]

    for i, text in enumerate(parts):
        body, headers = _signed(text, f"wamid.burst.{i}", phone)
        r = await live_app.post("/webhook/whatsapp", content=body, headers=headers)
        assert r.json()["accepted"] == 1
        if i < len(parts) - 1:
            await asyncio.sleep(0.3)  # 300ms apart, comfortably inside 2.5s

    await asyncio.sleep(4.0)  # window closes 2.5s after the last message

    inbound, outbound = await _counts(phone)
    assert inbound == 4, "every message must still be stored"
    assert outbound == 1, f"expected exactly one reply, got {outbound}"


async def test_the_merged_turn_contains_every_message(
    live_app, clean_conversation: str, monkeypatch
) -> None:
    """One reply is only correct if the turn actually saw all four messages."""
    phone = clean_conversation
    seen: list[str] = []

    from app.ingress import pipeline

    real = pipeline.compose_reply

    async def spy(conversation_id: str, turn_text: str) -> str:
        seen.append(turn_text)
        return await real(conversation_id, turn_text)

    monkeypatch.setattr(pipeline, "compose_reply", spy)

    for i, text in enumerate(["hi", "need loan", "5 lakh", "urgent bro"]):
        body, headers = _signed(text, f"wamid.merge.{i}", phone)
        await live_app.post("/webhook/whatsapp", content=body, headers=headers)
        await asyncio.sleep(0.1)

    await asyncio.sleep(1.5)

    assert len(seen) == 1, f"the model should be invoked once, was {len(seen)}"
    assert seen[0] == "hi\nneed loan\n5 lakh\nurgent bro"


# ================================================================== DEBOUNCE
async def test_a_lone_message_still_gets_a_reply(live_app, clean_conversation: str) -> None:
    """Debouncing must not mean 'never answers a single message'."""
    phone = clean_conversation
    body, headers = _signed("just one", "wamid.lone.1", phone)
    await live_app.post("/webhook/whatsapp", content=body, headers=headers)

    await asyncio.sleep(1.2)
    assert await _counts(phone) == (1, 1)


async def test_messages_outside_the_window_are_separate_turns(
    live_app, clean_conversation: str
) -> None:
    """Two genuinely separate thoughts deserve two replies."""
    phone = clean_conversation

    body, headers = _signed("first", "wamid.sep.1", phone)
    await live_app.post("/webhook/whatsapp", content=body, headers=headers)
    await asyncio.sleep(1.0)  # well past the 300ms test window

    body, headers = _signed("second", "wamid.sep.2", phone)
    await live_app.post("/webhook/whatsapp", content=body, headers=headers)
    await asyncio.sleep(1.0)

    assert await _counts(phone) == (2, 2)


async def test_the_hard_cap_forces_a_turn(live_app, clean_conversation: str, monkeypatch) -> None:
    """A user typing continuously must still get an answer.

    Messages arrive faster than the debounce window forever, so only the
    max-hold cap can end the turn.
    """
    s = get_settings()
    monkeypatch.setattr(s, "buffer_window_s", 0.5)
    monkeypatch.setattr(s, "buffer_max_hold_s", 0.9)

    phone = clean_conversation
    for i in range(8):
        body, headers = _signed(f"msg {i}", f"wamid.cap.{i}", phone)
        await live_app.post("/webhook/whatsapp", content=body, headers=headers)
        await asyncio.sleep(0.2)  # always inside the 0.5s window

    await asyncio.sleep(1.4)

    inbound, outbound = await _counts(phone)
    assert inbound == 8
    assert outbound >= 1, "max_hold must force a turn even while messages keep arriving"


# ============================================================== CANCELLATION
async def test_a_newer_message_cancels_an_in_flight_generation(
    live_app, clean_conversation: str, monkeypatch
) -> None:
    """The hard bit. A half-written reply must never ship."""
    s = get_settings()
    monkeypatch.setattr(s, "buffer_window_s", 0.2)
    monkeypatch.setattr(s, "buffer_max_hold_s", 1.0)
    # Generation now takes long enough that a new message lands mid-flight.
    monkeypatch.setattr(s, "fake_turn_latency_s", 1.2)

    phone = clean_conversation

    body, headers = _signed("first thought", "wamid.cancel.1", phone)
    await live_app.post("/webhook/whatsapp", content=body, headers=headers)

    await asyncio.sleep(0.6)  # window closed; generation is now in flight

    body, headers = _signed("actually, wait", "wamid.cancel.2", phone)
    await live_app.post("/webhook/whatsapp", content=body, headers=headers)

    await asyncio.sleep(3.0)

    inbound, outbound = await _counts(phone)
    assert inbound == 2
    assert outbound == 1, "the superseded generation must not have shipped a reply"


async def test_a_cancelled_turn_does_not_lose_the_customers_words(
    live_app, clean_conversation: str, monkeypatch
) -> None:
    """Drain-then-generate would silently drop 'first thought'."""
    s = get_settings()
    monkeypatch.setattr(s, "buffer_window_s", 0.2)
    monkeypatch.setattr(s, "buffer_max_hold_s", 1.0)
    monkeypatch.setattr(s, "fake_turn_latency_s", 0.9)

    phone = clean_conversation
    seen: list[str] = []

    from app.ingress import pipeline

    real = pipeline.compose_reply

    async def spy(conversation_id: str, turn_text: str) -> str:
        seen.append(turn_text)
        return await real(conversation_id, turn_text)

    monkeypatch.setattr(pipeline, "compose_reply", spy)

    body, headers = _signed("first thought", "wamid.keep.1", phone)
    await live_app.post("/webhook/whatsapp", content=body, headers=headers)
    await asyncio.sleep(0.5)

    body, headers = _signed("actually, wait", "wamid.keep.2", phone)
    await live_app.post("/webhook/whatsapp", content=body, headers=headers)

    await asyncio.sleep(3.0)

    assert seen, "a turn should have run"
    assert "first thought" in seen[-1], "the superseded message must survive into the next turn"
    assert "actually, wait" in seen[-1]


async def test_a_stale_generation_is_dropped_even_without_local_cancel(
    live_db, clean_conversation: str, monkeypatch
) -> None:
    """The multi-replica guard, isolated.

    Simulates the other replica: bump the generation counter in Redis directly
    while a turn is mid-generation. The local Task is never cancelled — only the
    counter check can stop this reply, which is exactly the situation the plan's
    note describes.
    """
    from app.ingress import repository

    s = get_settings()
    monkeypatch.setattr(s, "buffer_window_s", 0.15)
    monkeypatch.setattr(s, "fake_turn_latency_s", 0.8)

    phone = clean_conversation
    conv = await repository.get_or_create_conversation("whatsapp", customer_ref(phone))
    cid = str(conv["id"])

    from app.ingress.events import InboundEvent

    evt = InboundEvent(
        channel="whatsapp",
        provider_msg_id="wamid.replica.1",
        customer_ref=customer_ref(phone),
        text="hello",
        conversation_id=cid,
    )
    await coalesce.push(evt)

    # Let the window close and generation begin, then act as the other replica.
    await asyncio.sleep(0.5)
    await redis().incr(coalesce.k_gen(cid))

    await asyncio.sleep(2.0)

    _, outbound = await _counts(phone)
    assert outbound == 0, "a reply from a stale generation must be dropped"


# ==================================================================== LOCKING
async def test_only_one_holder_at_a_time(live_db) -> None:
    key = "tk:test:lock:1"
    await redis().delete(key)

    first = await lock.acquire(key, 5.0)
    second = await lock.acquire(key, 5.0)
    assert first is not None
    assert second is None, "a held lock must not be acquirable"

    assert await lock.release(key, first) is True
    third = await lock.acquire(key, 5.0)
    assert third is not None
    await lock.release(key, third)


async def test_release_is_compare_and_delete(live_db) -> None:
    """A holder whose TTL expired must not delete the next holder's lock."""
    key = "tk:test:lock:2"
    await redis().delete(key)

    mine = await lock.acquire(key, 5.0)
    assert mine is not None
    assert await lock.release(key, "not-my-token") is False, "wrong token must not release"
    assert await lock.release(key, mine) is True


async def test_a_locked_conversation_stands_down(live_db, clean_conversation: str, monkeypatch):
    """If another worker owns the conversation, this one must not also reply."""
    from app.ingress import repository
    from app.ingress.events import InboundEvent

    s = get_settings()
    monkeypatch.setattr(s, "buffer_window_s", 0.15)

    phone = clean_conversation
    conv = await repository.get_or_create_conversation("whatsapp", customer_ref(phone))
    cid = str(conv["id"])

    # Pretend another replica is mid-turn.
    held = await lock.acquire(coalesce.k_lock(cid), 5.0)
    assert held is not None

    await coalesce.push(
        InboundEvent(
            channel="whatsapp",
            provider_msg_id="wamid.locked.1",
            customer_ref=customer_ref(phone),
            text="hello",
            conversation_id=cid,
        )
    )
    await asyncio.sleep(1.0)

    _, outbound = await _counts(phone)
    assert outbound == 0, "must not reply while another worker holds the conversation"

    await lock.release(coalesce.k_lock(cid), held)


# ==================================================================== TYPING
async def test_typing_is_on_during_the_window_and_off_after(
    live_app, clean_conversation: str, monkeypatch
) -> None:
    from app.ingress import repository

    s = get_settings()
    monkeypatch.setattr(s, "buffer_window_s", 0.6)
    monkeypatch.setattr(s, "fake_turn_latency_s", 0.0)

    phone = clean_conversation
    body, headers = _signed("hi", "wamid.typing.1", phone)
    await live_app.post("/webhook/whatsapp", content=body, headers=headers)

    await asyncio.sleep(0.25)  # window still open
    conv = await repository.get_or_create_conversation("whatsapp", customer_ref(phone))
    cid = str(conv["id"])
    assert await coalesce.is_typing(cid) is True, "typing should show while the window is open"

    await asyncio.sleep(1.6)
    assert await coalesce.is_typing(cid) is False, "typing must clear once the reply is sent"
