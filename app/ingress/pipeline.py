"""The seam between "a turn is ready" and "here is what to say".

Phase 01 put a fixed acknowledgement here. Phase 02 changes the shape of the
call rather than its content: the input is now a *merged turn* — everything the
customer typed during the debounce window, joined — instead of one message.

Two properties this file has to keep, because Phase 03 drops a LangGraph
invocation into it and both get harder to add later:

  * **It composes, it does not send.** The caller sends, after re-checking that
    the turn is still current. If this function sent its own reply, a superseded
    turn would already have shipped by the time anyone noticed.
  * **It is cancellable.** Everything slow must be awaited, so that
    `Task.cancel()` lands promptly and a half-written reply never escapes.
"""

from __future__ import annotations

import asyncio

from app.logging import get_logger
from app.settings import get_settings

log = get_logger(__name__)

ACK_TEXT = (
    "Thanks for reaching out! I can help you compare loan options. "
    "What kind of loan are you looking for?"
)

UNSUPPORTED_TEXT = "I can only read text messages right now — could you type that out for me?"


async def compose_reply(conversation_id: str, turn_text: str) -> str:
    """Produce the reply for one merged turn. Must not send it.

    Phase 03 replaces the body with the compiled graph; the signature stays.
    """
    settings = get_settings()

    # Dev affordance: stand in for model latency so that cancelling an in-flight
    # generation is observable in the simulator rather than purely theoretical.
    if settings.fake_turn_latency_s > 0:
        await asyncio.sleep(settings.fake_turn_latency_s)

    if not turn_text.strip():
        # Every message in the burst was a voice note, image or location.
        return UNSUPPORTED_TEXT

    return ACK_TEXT
