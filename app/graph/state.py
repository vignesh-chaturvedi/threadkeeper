"""The state carried across nine days of silence.

Everything the funnel needs to resume lives in this dict. That is the point of
the checkpointer: turn 12 can be nine days after turn 11, in a different
container, on a different machine, and the conversation continues rather than
restarts.

Kept flat and JSON-serialisable on purpose — the checkpointer has to write it,
the replay tool has to diff it, and a human debugging a stuck conversation has
to read it.
"""

from __future__ import annotations

from typing import Any, TypedDict


class FunnelState(TypedDict, total=False):
    conversation_id: str
    stage: str

    # What we know, and how confident we are. Mirrored into the `slots` table
    # so it is queryable without deserialising a checkpoint.
    slots: dict[str, Any]
    # key -> 'extracted' | 'confirmed' | 'api'. Drives app.memory.conflict.
    slot_sources: dict[str, str]

    # {granted: bool, wording_hash: str, at: str}. Separate from slots because
    # consent is not a fact about the customer, it is a legal event with a
    # timestamp and an exact wording. Phase 07 promotes this to a ledger.
    consent: dict[str, Any]

    turn_text: str
    reply: str

    # Assembled by app.memory each turn and passed straight through. Not
    # checkpointed as truth — they are a *view* of state that is rebuilt from
    # the tables every turn, so a prompt change takes effect immediately rather
    # than only for conversations that start afterwards.
    history: list[dict[str, str]]
    profile_block: str
    recall_block: str
    # True only on the opening turn of a conversation with a known customer.
    returning: bool

    # Set by extraction, consumed by the policy, cleared each turn.
    interrupt: str | None
    escalate: bool

    # Decided by the policy before any node runs, so the routing decision is a
    # value that can be logged and asserted on rather than control flow.
    next_stage: str
    route_reason: str
    holds_stage: bool

    # Rolling accounting, so cost-per-conversation is available in Phase 10.
    usage: dict[str, int]


def new_state(conversation_id: str, stage: str = "intent_route") -> FunnelState:
    return FunnelState(
        conversation_id=conversation_id,
        stage=stage,
        slots={},
        slot_sources={},
        consent={},
        turn_text="",
        reply="",
        interrupt=None,
        escalate=False,
        next_stage=stage,
        route_reason="new_conversation",
        holds_stage=False,
        usage={"input_tokens": 0, "output_tokens": 0, "calls": 0},
    )
