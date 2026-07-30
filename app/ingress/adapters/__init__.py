"""Adapter registry. One line to add a channel."""

from __future__ import annotations

from app.ingress.adapters.base import (
    ChannelAdapter,
    PermanentSendError,
    SendError,
    TransientSendError,
)
from app.ingress.adapters.whatsapp import WhatsAppAdapter

_ADAPTERS: dict[str, ChannelAdapter] = {
    "whatsapp": WhatsAppAdapter(),
}


def get_adapter(channel: str) -> ChannelAdapter:
    try:
        return _ADAPTERS[channel]
    except KeyError:
        raise ValueError(f"unknown channel: {channel!r}") from None


__all__ = [
    "ChannelAdapter",
    "PermanentSendError",
    "SendError",
    "TransientSendError",
    "WhatsAppAdapter",
    "get_adapter",
]
