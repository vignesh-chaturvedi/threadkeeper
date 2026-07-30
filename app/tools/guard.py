"""Which tools are callable, when.

The plan states the requirement precisely: *"create_application before consent
must be impossible in code, not discouraged in a prompt."* This module is where
that impossibility lives.

Two layers, because one is not enough:

  1. **Stage scope.** A tool the current stage does not permit is never even
     offered to the model, and is refused if called anyway. Not offering it
     prevents most attempts; refusing it handles the rest.

  2. **Preconditions.** Independent of stage, some tools have conditions that
     must hold — `create_application` requires granted consent, full stop. A
     stage table alone would be defeated by anything that moved the stage.

The distinction matters under adversarial pressure, which Phase 08 applies
deliberately: prompt injection can talk a model into *trying* a tool. It cannot
talk a Python function into returning a different answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Stage -> tools permitted at that stage. Absent stages permit nothing beyond
# the always-allowed set below.
ALLOWED: dict[str, frozenset[str]] = {
    "intent_route": frozenset(),
    "qualify": frozenset({"check_eligibility"}),
    # Nothing during consent. The customer is being asked a question; calling a
    # lender while they decide is exactly the overreach the stage exists to stop.
    "consent": frozenset(),
    "kyc_collect": frozenset({"verify_pan"}),
    "offer_match": frozenset({"fetch_offers", "check_eligibility"}),
    "close": frozenset({"create_application", "schedule_followup"}),
    "escalate": frozenset(),
    "handle_objection": frozenset(),
    "handle_off_topic": frozenset(),
}

# Handing off to a human must work from anywhere — the same reasoning that puts
# opt-out first in the stage policy. Follow-ups likewise: deferring is always a
# legitimate move.
ALWAYS_ALLOWED: frozenset[str] = frozenset({"escalate_to_human", "schedule_followup"})

# Tools that change something outside this system. They need idempotency keys,
# and they are the ones worth auditing.
WRITE_TOOLS: frozenset[str] = frozenset({"create_application", "schedule_followup"})


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    reason: str = "ok"

    def __bool__(self) -> bool:
        return self.allowed


def tools_for_stage(stage: str) -> frozenset[str]:
    """What the model may be told about at this stage."""
    return ALLOWED.get(stage, frozenset()) | ALWAYS_ALLOWED


def check(tool: str, stage: str, state: dict[str, Any]) -> Verdict:
    """The single decision point. Every tool call goes through here."""
    if tool not in tools_for_stage(stage):
        return Verdict(False, f"tool_not_allowed_at_stage:{stage}")

    slots = state.get("slots") or {}
    consent = state.get("consent") or {}

    # --- preconditions, independent of stage -----------------------------
    if slots.get("opted_out"):
        # Nothing outward-facing after an opt-out, whatever the stage says.
        if tool in WRITE_TOOLS:
            return Verdict(False, "customer_opted_out")

    if tool == "create_application":
        if not consent.get("granted"):
            return Verdict(False, "consent_missing")
        if not slots.get("pan_status") == "available":
            return Verdict(False, "kyc_incomplete")

    if tool == "fetch_offers" and not consent.get("granted"):
        # Fetching offers means sending the customer's details to lenders.
        # That is the thing consent is consent *for*.
        return Verdict(False, "consent_missing")

    return Verdict(True)
