"""WhatsApp Cloud API adapter.

Two transports sit behind the same adapter:
  * mock  — the default. Accepts the send, returns a synthetic wamid, and can be
            told to fail a fraction of the time so the retry path is exercised
            rather than assumed.
  * whatsapp — a real Graph API call. Never used in this project (no BSP
            account, no real PII by design) but written so the mock is visibly a
            stand-in rather than the only thing that exists.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from typing import Any

import httpx

from app.ingress.adapters.base import PermanentSendError, TransientSendError
from app.ingress.events import InboundEvent, OutboundMessage
from app.logging import get_logger
from app.privacy.refs import customer_ref
from app.settings import get_settings

log = get_logger(__name__)

SIGNATURE_HEADER = "x-hub-signature-256"
SUPPORTED_KINDS = {"text"}


class WhatsAppAdapter:
    name = "whatsapp"

    # ---------------------------------------------------------------- inbound
    def verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        settings = get_settings()
        if not settings.verify_signatures:
            # Local dev with no secret configured. Loud, so it cannot become the
            # accidental production posture — and settings refuses to boot
            # staging/prod without a secret anyway.
            log.warning("signature_check_skipped", reason="no_app_secret_configured")
            return True

        provided = {k.lower(): v for k, v in headers.items()}.get(SIGNATURE_HEADER, "")
        if not provided.startswith("sha256="):
            return False

        expected = hmac.new(
            settings.whatsapp_app_secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        # compare_digest, not ==, so a timing side channel can't leak the digest.
        return hmac.compare_digest(expected, provided.removeprefix("sha256="))

    def parse(self, payload: dict[str, Any]) -> list[InboundEvent]:
        events: list[InboundEvent] = []

        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}

                # Delivery/read receipts land on the same endpoint. Not an error.
                if "messages" not in value:
                    if "statuses" in value:
                        log.debug("status_callback_ignored", count=len(value["statuses"]))
                    continue

                for msg in value["messages"]:
                    events.append(self._parse_one(msg))

        return events

    def _parse_one(self, msg: dict[str, Any]) -> InboundEvent:
        kind = msg.get("type", "text")
        sender = msg.get("from", "")

        if kind in SUPPORTED_KINDS:
            text = (msg.get("text") or {}).get("body", "")
        else:
            # Images, voice notes, locations, buttons. Phase 03 replies asking
            # for text; dropping them silently would look like the bot ignoring
            # the customer.
            text = ""

        sent_at = None
        if ts := msg.get("timestamp"):
            with_suppressed_error = str(ts)
            if with_suppressed_error.isdigit():
                sent_at = datetime.fromtimestamp(int(with_suppressed_error), tz=UTC)

        return InboundEvent(
            channel=self.name,
            provider_msg_id=msg.get("id", ""),
            customer_ref=customer_ref(sender),
            text=text,
            kind="text" if kind in SUPPORTED_KINDS else "unsupported",
            sent_at=sent_at,
            raw=msg,
        )

    # --------------------------------------------------------------- outbound
    async def send(self, message: OutboundMessage) -> str:
        settings = get_settings()
        if settings.outbound_transport == "mock":
            return await self._send_mock(message)
        return await self._send_cloud_api(message)

    async def _send_mock(self, message: OutboundMessage) -> str:
        settings = get_settings()
        if settings.outbound_failure_rate > 0 and secrets.randbelow(10_000) < int(
            settings.outbound_failure_rate * 10_000
        ):
            raise TransientSendError("injected transient failure (mock transport)")
        return f"wamid.mock.{secrets.token_hex(8)}"

    async def _send_cloud_api(self, message: OutboundMessage) -> str:
        settings = get_settings()
        url = f"{settings.whatsapp_api_base}/{settings.whatsapp_phone_number_id}/messages"
        body: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": message.customer_ref,
            "type": "text",
            "text": {"body": message.text},
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
                )
        except httpx.TimeoutException as exc:
            raise TransientSendError(f"timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TransientSendError(f"transport error: {exc}") from exc

        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientSendError(f"provider {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise PermanentSendError(f"provider {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        return (data.get("messages") or [{}])[0].get("id", "")
