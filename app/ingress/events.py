"""Channel-agnostic message types.

Nothing downstream of the adapter — not the buffer, not the graph, not the
scheduler — is allowed to know what WhatsApp's payload looks like. That seam is
the whole point: "every customer channel" is a product claim, and it only holds
if the shape of a turn is independent of the transport that delivered it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

MessageKind = Literal["text", "unsupported"]


class InboundEvent(BaseModel):
    """One customer message, normalised."""

    channel: str
    provider_msg_id: str
    customer_ref: str
    text: str
    kind: MessageKind = "text"
    sent_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    # Populated once the conversation row is resolved.
    conversation_id: str | None = None


class OutboundMessage(BaseModel):
    """One agent message, before a transport has touched it."""

    channel: str
    conversation_id: str
    customer_ref: str
    text: str
    # Phase 06 sets this for nudges outside the 24h customer-service window.
    template_name: str | None = None


class SendResult(BaseModel):
    provider_msg_id: str | None = None
    attempts: int = 1
