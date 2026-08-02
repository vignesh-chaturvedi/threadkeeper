"""The auditable core: where the funnel is allowed to go next.

This module contains no model call, no I/O and no framework. It is a pure
function from state to a destination plus the reason it was chosen, which means
every routing rule in the product can be unit-tested in microseconds and read
by someone who has never seen LangGraph.

That is the whole argument of the project. A prompt that says "advance to KYC
once you have income and product" fails silently and differently every time. The
same rule in code fails loudly, once, in a test. In a regulated flow — consent
before data sharing, KYC before application — "usually correct" is not a
category that exists.

`reason` is not decoration. It is written to `stage_transitions` on every move,
so months later the answer to "why did this conversation jump to escalate" is a
row, not a reconstruction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The funnel, in order. Membership here is what makes a stage legal.
STAGES = (
    "intent_route",
    "qualify",
    "consent",
    "kyc_collect",
    "offer_match",
    "close",
)
TERMINAL = ("close", "escalate")

# Interrupt handlers. These answer the customer without advancing the funnel —
# the stage they return to is whatever the stage machine says comes next.
INTERRUPT_NODES = ("handle_objection", "handle_off_topic")


@dataclass(frozen=True, slots=True)
class Decision:
    stage: str
    reason: str
    # An interrupt is answered *without* moving the funnel forward. Recording
    # both lets the transition log distinguish "advanced" from "diverted".
    holds_stage: bool = False


def decide(state: dict[str, Any]) -> Decision:
    """Deterministic. No model call. This is the auditable core.

    Order matters and is itself a policy statement:

      1. **Opt-out beats everything.** Under the DPDP Act consent is revocable,
         and a withdrawal that waits its turn behind a KYC prompt is not a
         withdrawal. It must work at any stage, on the same turn.
      2. **Escalation beats progress.** A customer asking for a human should not
         be walked through another qualifying question first.
      3. **Objections and off-topic pause, they do not advance.** Real
         conversations are not straight lines; answering "what's the interest
         rate" should not count as completing the consent step.
      4. Then, and only then, the funnel's own gates — each of which is an
         explicit precondition rather than a hope about the prompt.
    """
    slots = state.get("slots") or {}
    consent = state.get("consent") or {}
    interrupt = state.get("interrupt")

    # --- 1. opt-out, unconditionally ------------------------------------
    if slots.get("opted_out") or interrupt == "opt_out":
        return Decision("close", "customer_opted_out")

    # --- 2. escalation ---------------------------------------------------
    if state.get("escalate") or interrupt == "escalate":
        return Decision("escalate", "escalation_requested")

    # --- 3. interrupts that pause rather than advance --------------------
    if interrupt == "objection":
        return Decision("handle_objection", "objection_raised", holds_stage=True)
    if interrupt == "off_topic":
        return Decision("handle_off_topic", "off_topic", holds_stage=True)

    # --- 4. the funnel's gates -------------------------------------------
    if not slots.get("product"):
        return Decision("qualify", "product_unknown")

    if consent.get("granted") is False:
        # Explicitly refused, as opposed to not yet asked. Re-prompting someone
        # who already said no is how a sales bot becomes a complaint.
        return Decision("close", "consent_refused")

    if not consent.get("granted"):
        return Decision("consent", "consent_missing")

    if not slots.get("income_band"):
        return Decision("qualify", "income_band_missing")

    if not slots.get("pan_status"):
        return Decision("kyc_collect", "pan_status_missing")

    if slots.get("pan_status") == "missing":
        # Known, and known to be absent. The funnel cannot proceed to a lender
        # application, but this is not a dead conversation either.
        return Decision("kyc_collect", "pan_not_available")

    # The funnel had no way to finish successfully until the Phase 10 funnel
    # chart made it obvious: `close` was reachable only by opt-out or consent
    # refusal, so every close in the system was a failure and "cost per closed
    # sale" had no numerator. Accepting an offer is what completes a lending
    # funnel, and it needs both halves — an acceptance *and* an offer that was
    # actually shown. "haan" before any figures have been quoted is agreement to
    # nothing, and turning it into an application is precisely the overreach the
    # rest of this policy exists to prevent.
    if slots.get("offer_accepted") and state.get("last_offer"):
        return Decision("close", "offer_accepted")

    return Decision("offer_match", "ready_for_offers")


def is_terminal(stage: str) -> bool:
    return stage in TERMINAL


def is_valid(stage: str) -> bool:
    return stage in STAGES or stage in TERMINAL or stage in INTERRUPT_NODES
