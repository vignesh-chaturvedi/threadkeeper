"""Tokenize on the way in. Detokenize at exactly one place on the way out.

The plan's shape is "tokenize before the model call". This goes one step
further and tokenizes **before the message is stored at all**, because "the
model never sees a PAN" is a much weaker claim than "a PAN is never on disk in
the clear". The second is the one a bank's security review asks about, and it is
the one `tests/test_privacy.py` asserts across every table.

So the flow is:

    inbound  →  tokenize  →  messages, slots, checkpoints, logs, tools
                              (all hold `[PAN_a1b2c3d4e5]`, never digits)

    outbound →  detokenize →  the customer
                  ↑
                  the only place values come back, plus a short allow-list of
                  tools that genuinely need the real thing (verify_pan).

Everything between those two points works on tokens and cannot leak what it does
not have.
"""

from __future__ import annotations

import re
from typing import Any

from app.logging import get_logger
from app.privacy import patterns, vault

log = get_logger(__name__)

TOKEN_PATTERN = re.compile(r"\[(PAN|AADHAAR|PHONE|ACCOUNT|IFSC)_[0-9a-f]{10}\]")


async def tokenize(text: str, conversation_id: str) -> tuple[str, dict[str, str]]:
    """Returns (safe text, {token: kind}).

    Replaces right-to-left so earlier spans keep their offsets.
    """
    detections = patterns.detect(text)
    if not detections:
        return text, {}

    mapping: dict[str, str] = {}
    out = text
    for detection in reversed(detections):
        token = await vault.put(conversation_id, detection.kind, detection.value)
        out = out[: detection.start] + token + out[detection.end :]
        mapping[token] = detection.kind

    log.info(
        "tokenized",
        conversation_id=conversation_id,
        # The kinds, never the values, and never a count that could be
        # correlated back to a specific message.
        kinds=sorted({d.kind for d in detections}),
    )
    return out, mapping


async def detokenize(text: str, conversation_id: str | None = None) -> str:
    """Put the real values back. The narrow choke point.

    Called from exactly two places — the outbound sender, and the tool client
    for tools on the allow-list. Anything else calling this is a finding.
    """
    tokens = TOKEN_PATTERN.findall(text)
    if not tokens:
        return text

    found = TOKEN_PATTERN.finditer(text)
    wanted = [m.group(0) for m in found]
    values = await vault.get_many(wanted)

    out = text
    for token, value in values.items():
        out = out.replace(token, value)

    missing = [t for t in wanted if t not in values]
    if missing:
        # A dangling token means the vault was erased — a right-to-erasure
        # request. Leaving the token visible is correct: the value is gone.
        log.warning("detokenize_missing", tokens=len(missing))
    return out


def has_tokens(text: str) -> bool:
    return bool(TOKEN_PATTERN.search(text))


def token_kinds(text: str) -> list[str]:
    return sorted({m.group(1) for m in TOKEN_PATTERN.finditer(text)})


def scrub_event(_logger: Any, _name: str, event: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: no identifier ever reaches a log line.

    Belt and braces. By the time anything is logged the text should already be
    tokenized, but a log statement is exactly the sort of thing added in a hurry
    at 2am, and the failure mode is a PAN in a log aggregator forever.
    """
    for key, value in event.items():
        if isinstance(value, str) and len(value) >= 10:
            event[key] = patterns.scrub(value)
    return event
