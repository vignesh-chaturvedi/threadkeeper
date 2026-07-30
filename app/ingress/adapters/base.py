"""The channel adapter contract.

Adding a channel means implementing this and registering it. Everything else in
the system stays untouched — which is the claim the interface exists to make
credible.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.ingress.events import InboundEvent, OutboundMessage


class SendError(Exception):
    """Base for transport failures."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class TransientSendError(SendError):
    """Timeout, 5xx, rate limit — worth retrying."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class PermanentSendError(SendError):
    """Bad recipient, unapproved template, revoked token — retrying is pointless."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


@runtime_checkable
class ChannelAdapter(Protocol):
    """A transport that can receive and send customer messages."""

    name: str

    def verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        """Reject payloads that did not come from the provider."""
        ...

    def parse(self, payload: dict[str, Any]) -> list[InboundEvent]:
        """Normalise a provider webhook body into zero or more InboundEvents.

        Zero is normal and must not be an error: delivery receipts, read
        receipts and status callbacks all arrive on the same endpoint.
        """
        ...

    async def send(self, message: OutboundMessage) -> str:
        """Deliver one message. Returns the provider's message id.

        Raises TransientSendError or PermanentSendError; the caller owns retry
        policy so that every channel gets the same backoff and dead-lettering.
        """
        ...
