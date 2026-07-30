"""Inbound webhook.

Three properties, in priority order:

  1. **Authentic.** Reject anything without a valid HMAC before parsing it.
  2. **Idempotent.** The database decides whether a message is new. Providers
     redeliver on any ack slower than their timeout, and a redelivery that
     produces a second reply is the most common bug in production chat systems.
  3. **Fast.** Return 200 as soon as the message is durably stored. A slow ack
     makes the provider retry, which multiplies load at exactly the moment the
     system is already struggling.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response

from app.buffer import coalesce
from app.ingress import repository
from app.ingress.adapters import get_adapter
from app.logging import bind_contextvars, get_logger
from app.settings import get_settings

log = get_logger(__name__)

router = APIRouter(prefix="/webhook", tags=["ingress"])


@router.get("/whatsapp")
async def verify_subscription(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
) -> Response:
    """Meta's subscription handshake: echo the challenge, verbatim, as text."""
    settings = get_settings()
    if mode == "subscribe" and token == settings.whatsapp_verify_token:
        log.info("webhook_subscription_verified")
        return Response(content=challenge, media_type="text/plain")
    log.warning("webhook_subscription_rejected", mode=mode)
    raise HTTPException(status_code=403, detail="verification failed")


@router.post("/whatsapp")
async def inbound(req: Request, bg: BackgroundTasks) -> dict[str, Any]:
    adapter = get_adapter("whatsapp")
    raw = await req.body()

    if not adapter.verify_signature(raw, dict(req.headers)):
        log.warning("webhook_bad_signature", bytes=len(raw))
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # 400, not 500: the provider should not retry a body we can never parse.
        raise HTTPException(status_code=400, detail="malformed json") from None

    events = adapter.parse(payload)
    if not events:
        # Delivery receipts, read receipts, status callbacks. Perfectly normal.
        return {"ok": True, "accepted": 0}

    accepted = 0
    duplicates = 0

    for evt in events:
        if not evt.provider_msg_id:
            log.warning("webhook_event_without_id")
            continue

        conversation = await repository.get_or_create_conversation(evt.channel, evt.customer_ref)
        conversation_id = str(conversation["id"])
        evt.conversation_id = conversation_id
        bind_contextvars(conversation_id=conversation_id)

        message_id = await repository.record_inbound(evt, conversation_id)
        if message_id is None:
            # The unique index rejected it — we have already handled this one.
            duplicates += 1
            log.info("duplicate_webhook", provider_msg_id=evt.provider_msg_id)
            continue

        accepted += 1
        log.info("inbound_accepted", provider_msg_id=evt.provider_msg_id, message_id=message_id)
        # Ack now, think later. The buffer decides *when* the thinking happens —
        # a burst of messages becomes one turn rather than one turn each.
        bg.add_task(coalesce.push, evt)

    return {"ok": True, "accepted": accepted, "duplicates": duplicates}
