"""The conflict rule, written down.

The plan asks for exactly one decision here — "newer confirmed value beats older
extracted value" — and asks for it to be deliberate rather than emergent. The
full rule, in order:

  1. **Provenance outranks recency.** A value the customer confirmed, or one a
     lender API returned, beats one inferred from a passing remark, however
     recent the inference. Otherwise a stray "5 lakh" in an unrelated sentence
     silently overwrites an amount the customer explicitly agreed to.

  2. **Within the same provenance, newer wins.** Customer said 4 lakh, now says
     6 — that is a correction, not noise.

  3. **Some values never degrade.** Withdrawing consent and opting out are
     decisions, not observations. A later turn that merely fails to repeat them
     is silence, not reversal — and treating silence as re-consent is how a
     system ends up messaging someone who asked it to stop.

Rule 3 is the one with legal weight, which is why it is a hard gate rather than
a tiebreak.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# Higher wins. 'confirmed' means the customer said it back to us; 'api' means a
# lender returned it; 'extracted' means a model read it out of a message.
SOURCE_RANK: dict[str, int] = {
    "extracted": 0,
    "api": 1,
    "confirmed": 2,
}

# Values that a later, weaker observation may never overturn.
STICKY: frozenset[str] = frozenset({"opted_out", "consent", "consent_granted"})


@dataclass(frozen=True, slots=True)
class SlotValue:
    value: Any
    source: str = "extracted"
    confidence: float = 1.0
    updated_at: datetime | None = None

    @property
    def rank(self) -> int:
        return SOURCE_RANK.get(self.source, 0)


@dataclass(frozen=True, slots=True)
class Resolution:
    winner: SlotValue
    reason: str
    changed: bool


def resolve(key: str, existing: SlotValue | None, incoming: SlotValue) -> Resolution:
    """Decide which of two competing values for one slot survives."""
    if existing is None:
        return Resolution(incoming, "first_value", changed=True)

    if incoming.value is None:
        return Resolution(existing, "incoming_empty", changed=False)

    # --- rule 3: sticky decisions -------------------------------------
    # Only a *confirmed* signal may reverse one. Not a rank comparison: an
    # equally-ranked confirmation must be able to withdraw consent, or consent
    # is not revocable, which is the one property the DPDP Act actually
    # requires. But an extraction returning False almost always means "not
    # mentioned this turn" rather than "they asked to resume", and treating
    # that as re-consent is how a system messages someone who said stop.
    if key in STICKY and _is_truthy(existing.value) and not _is_truthy(incoming.value):
        if incoming.source != "confirmed":
            return Resolution(existing, "sticky_not_downgraded", changed=False)
        return Resolution(incoming, "sticky_withdrawn_by_confirmation", changed=True)

    if existing.value == incoming.value:
        # Same answer from a better source is still an upgrade worth recording:
        # it changes what a future weaker observation is allowed to do.
        if incoming.rank > existing.rank:
            return Resolution(incoming, "provenance_upgraded", changed=True)
        return Resolution(existing, "unchanged", changed=False)

    # --- rule 1: provenance outranks recency --------------------------
    if incoming.rank > existing.rank:
        return Resolution(incoming, "stronger_provenance", changed=True)
    if incoming.rank < existing.rank:
        return Resolution(existing, "weaker_provenance_rejected", changed=False)

    # --- rule 2: same provenance, newer wins --------------------------
    if _at(incoming) >= _at(existing):
        return Resolution(incoming, "newer_same_provenance", changed=True)
    return Resolution(existing, "older_rejected", changed=False)


def _is_truthy(value: Any) -> bool:
    """Consent is a dict; opted_out is a bool. Both mean 'a decision was made'."""
    if isinstance(value, dict):
        return bool(value.get("granted"))
    return bool(value)


# An unknown timestamp means "we did not record when this was learned", which
# for a value already in state means a previous turn. Defaulting it to *now*
# instead — as this did originally — makes the stored value always appear newer
# than the one arriving, so every correction is rejected and "4 lakh, sorry, 6
# lakh" silently keeps 4.
_UNKNOWN_TIME = datetime.min.replace(tzinfo=UTC)


def _at(slot: SlotValue) -> datetime:
    return slot.updated_at or _UNKNOWN_TIME
