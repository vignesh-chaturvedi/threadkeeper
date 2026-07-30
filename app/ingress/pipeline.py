"""The seam between "a message arrived" and "the agent thinks about it".

Phase 01 answers with a fixed acknowledgement — there is no model, no stage
machine and no memory yet, and pretending otherwise would make the idempotency
result untrustworthy. What this file establishes is the contract:

    webhook (fast, synchronous, returns 200)
        └── handle_inbound (background, owns its own failures)

Phase 02 replaces the body of handle_inbound with buffer.push(evt), which
debounces a burst into a single turn. Phase 03 puts the graph behind that. The
signature does not change.
"""

from __future__ import annotations

from app.ingress import outbound, repository
from app.ingress.events import InboundEvent, OutboundMessage
from app.logging import get_logger, log_context

log = get_logger(__name__)

ACK_TEXT = (
    "Thanks for reaching out! I can help you compare loan options. "
    "What kind of loan are you looking for?"
)

UNSUPPORTED_TEXT = "I can only read text messages right now — could you type that out for me?"


async def handle_inbound(evt: InboundEvent) -> None:
    """Process one already-deduplicated inbound message.

    Never raises. This runs detached from the request that scheduled it, so an
    exception here would be logged by asyncio and otherwise vanish; failures are
    handled and recorded instead.
    """
    conversation_id = evt.conversation_id
    assert conversation_id is not None, "conversation must be resolved before handling"

    with log_context(conversation_id=conversation_id, channel=evt.channel):
        try:
            reply = UNSUPPORTED_TEXT if evt.kind == "unsupported" else ACK_TEXT
            await outbound.send(
                OutboundMessage(
                    channel=evt.channel,
                    conversation_id=conversation_id,
                    customer_ref=evt.customer_ref,
                    text=reply,
                )
            )
            await repository.touch_last_inbound(conversation_id)
        except Exception:
            log.exception("inbound_handling_failed", provider_msg_id=evt.provider_msg_id)
