"""Outbound send with retry, backoff and a dead-letter table.

Policy lives here rather than in each adapter, so every channel inherits the
same behaviour: retry transient failures with exponential backoff plus jitter,
never retry permanent ones, and when attempts are exhausted write the message to
outbound_dead_letters instead of dropping it.

Jitter matters more than it looks. Without it, a provider blip that fails a
hundred conversations at once produces a hundred retries that fire in the same
millisecond, which is how a recoverable blip becomes an outage.
"""

from __future__ import annotations

import asyncio
import secrets

from app.ingress import repository
from app.ingress.adapters import PermanentSendError, SendError, TransientSendError, get_adapter
from app.ingress.events import OutboundMessage, SendResult
from app.logging import get_logger
from app.privacy import tokenize
from app.settings import get_settings

log = get_logger(__name__)


def _jittered(delay: float) -> float:
    """Full jitter: uniform in [0, delay]. Cheap, and it spreads a thundering herd."""
    return delay * (secrets.randbelow(1_000) / 1_000)


async def send(message: OutboundMessage) -> SendResult:
    """Deliver a message, persist it, and never raise on failure.

    A send failure must not propagate into the turn that produced it — the reply
    is already generated and the customer's state has moved on. Failures become
    dead letters, which are visible, instead of exceptions, which are not.
    """
    settings = get_settings()
    adapter = get_adapter(message.channel)

    # The narrow choke point. Everything upstream of here works on tokens; this
    # is where — and the only where — the customer's own values are restored,
    # because they are the one party entitled to see them.
    message = message.model_copy(
        update={"text": await tokenize.detokenize(message.text, message.conversation_id)}
    )
    max_attempts = settings.outbound_max_attempts
    backoff = settings.outbound_backoff_s

    last_error = "unknown"

    for attempt in range(1, max_attempts + 1):
        try:
            provider_msg_id = await adapter.send(message)
        except PermanentSendError as exc:
            log.error(
                "outbound_permanent_failure",
                conversation_id=message.conversation_id,
                attempt=attempt,
                error=str(exc),
            )
            await repository.record_dead_letter(
                message.conversation_id, message.text, attempt, str(exc)
            )
            return SendResult(attempts=attempt)
        except (TransientSendError, SendError) as exc:
            last_error = str(exc)
            log.warning(
                "outbound_transient_failure",
                conversation_id=message.conversation_id,
                attempt=attempt,
                max_attempts=max_attempts,
                error=last_error,
            )
            if attempt < max_attempts:
                delay = backoff[min(attempt - 1, len(backoff) - 1)]
                await asyncio.sleep(_jittered(delay))
                continue
        else:
            await repository.record_outbound(message.conversation_id, message.text, provider_msg_id)
            log.info(
                "outbound_sent",
                conversation_id=message.conversation_id,
                provider_msg_id=provider_msg_id,
                attempts=attempt,
            )
            return SendResult(provider_msg_id=provider_msg_id, attempts=attempt)

    log.error(
        "outbound_dead_lettered",
        conversation_id=message.conversation_id,
        attempts=max_attempts,
        error=last_error,
    )
    await repository.record_dead_letter(
        message.conversation_id, message.text, max_attempts, last_error
    )
    return SendResult(attempts=max_attempts)
