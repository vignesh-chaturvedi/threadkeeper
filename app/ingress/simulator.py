"""A fake WhatsApp client, mounted at /sim in local and test environments.

Built on day two rather than day twelve because every phase after this one needs
a way to push traffic at the webhook: Phase 02 needs bursts, Phase 03 needs long
multi-turn funnels, Phase 06 needs a conversation it can abandon and come back
to, Phase 12 needs something worth filming.

Design note: this endpoint *signs* a payload and hands it back to the browser.
It does not post it. The browser makes the real HTTP call to /webhook/whatsapp,
which means the demo exercises the genuine ingress path — signature check
included — and a "resend" button is literally a byte-identical redelivery.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.buffer import coalesce
from app.ingress import repository
from app.privacy.refs import customer_ref, normalise_msisdn
from app.settings import get_settings

router = APIRouter(prefix="/sim", tags=["simulator"])

_HTML = Path(__file__).parent / "static" / "sim.html"


class PayloadRequest(BaseModel):
    phone: str = Field(default="919876543210")
    text: str
    # Supplying an id makes the payload byte-identical to a previous one, which
    # is how the UI demonstrates redelivery.
    msg_id: str | None = None


class PayloadResponse(BaseModel):
    payload: dict[str, Any]
    signature: str
    msg_id: str


def _guard() -> None:
    if not get_settings().enable_simulator:
        raise HTTPException(status_code=404, detail="simulator disabled")


def build_payload(phone: str, text: str, msg_id: str) -> dict[str, Any]:
    """A minimal but faithful WhatsApp Cloud API `messages` webhook body."""
    wa_id = normalise_msisdn(phone)
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "0",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "911234567890",
                                "phone_number_id": "sim",
                            },
                            "contacts": [{"profile": {"name": "Simulator"}, "wa_id": wa_id}],
                            "messages": [
                                {
                                    "from": wa_id,
                                    "id": msg_id,
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def sign(body: bytes) -> str:
    secret = get_settings().whatsapp_app_secret
    if not secret:
        return ""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


@router.get("", response_class=HTMLResponse)
async def ui() -> HTMLResponse:
    _guard()
    return HTMLResponse(_HTML.read_text(encoding="utf-8"))


@router.post("/api/payload", response_model=PayloadResponse)
async def make_payload(req: PayloadRequest) -> PayloadResponse:
    """Returns a signed, ready-to-post webhook body."""
    _guard()
    msg_id = req.msg_id or f"wamid.sim.{secrets.token_hex(8)}"
    payload = build_payload(req.phone, req.text, msg_id)
    # This must serialise byte-for-byte the way the browser's JSON.stringify
    # will, or the HMAC it computes over won't be the HMAC the webhook checks.
    # ensure_ascii=False is the one that matters: Python escapes non-ASCII by
    # default and JavaScript does not, so "PAN nahi hai" survives but
    # "ब्याज दर" would silently start failing signature verification — which is
    # exactly the traffic Phase 09 is about.
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return PayloadResponse(payload=payload, signature=sign(body), msg_id=msg_id)


@router.get("/api/thread")
async def get_thread(phone: str = "919876543210") -> dict[str, Any]:
    """The transcript as the customer would see it, plus any dead letters."""
    _guard()
    ref = customer_ref(phone)
    conversation = await repository.get_or_create_conversation("whatsapp", ref)
    messages = await repository.thread(str(conversation["id"]))
    return {
        "conversation_id": str(conversation["id"]),
        "typing": await coalesce.is_typing(str(conversation["id"])),
        "customer_ref": ref,
        "stage": conversation["stage"],
        "status": conversation["status"],
        "messages": [
            {
                "id": m["id"],
                "direction": m["direction"],
                "body": m["body"],
                "at": m["received_at"].isoformat(),
            }
            for m in messages
        ],
    }


@router.get("/api/buffer")
async def buffer_state(phone: str = "919876543210") -> dict[str, Any]:
    """Live view of the debounce window — the whole point of Phase 02, made visible."""
    _guard()
    conversation = await repository.get_or_create_conversation("whatsapp", customer_ref(phone))
    return await coalesce.pending(str(conversation["id"]))


@router.post("/api/reset")
async def reset(phone: str = "919876543210") -> dict[str, Any]:
    """Wipes one simulated customer so a demo can be re-run from zero."""
    _guard()
    from app import db
    from app.cache import redis

    ref = customer_ref(phone)
    conversation = await repository.get_or_create_conversation("whatsapp", ref)
    cid = str(conversation["id"])

    # Redis state has to go too, or a reset conversation inherits the previous
    # one's generation counter and buffered text.
    await redis().delete(
        coalesce.k_buf(cid),
        coalesce.k_gen(cid),
        coalesce.k_deadline(cid),
        coalesce.k_first(cid),
        coalesce.k_lock(cid),
        coalesce.k_typing(cid),
    )
    deleted = await db.execute(
        "DELETE FROM conversations WHERE channel = 'whatsapp' AND customer_ref = %s", ref
    )
    return {"ok": True, "deleted_conversations": deleted}
