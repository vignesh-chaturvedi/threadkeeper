"""Three tiers of memory, and an opinion about which one matters.

    tier 1  working    last turns, trimmed to a token budget
    tier 2  profile    structured slots, rendered as compact facts
    tier 3  semantic   pgvector over per-conversation summaries

The opinion: **tier 2 does most of the work.** Retrieval answers "what did this
customer complain about last time"; it does not answer "what is their income",
and a funnel is mostly made of the second kind of question. Tier 3 is built,
scoped narrowly to cross-sell against prior objections, and measured — see
`evals/memory_ab.py` — rather than assumed essential because it is the
fashionable part.

This module is the single call the graph makes. Which tiers are consulted, and
in what order, is a decision that belongs here rather than scattered across
nodes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app import db
from app.logging import get_logger
from app.memory import conflict, profile, semantic, tokens
from app.settings import get_settings

log = get_logger(__name__)


@dataclass(slots=True)
class Recollection:
    """Everything the model is told about a conversation, in one object."""

    history: list[dict[str, str]] = field(default_factory=list)
    profile_block: str = ""
    recall_block: str = ""
    tokens_used: int = 0
    tiers: list[str] = field(default_factory=list)
    # Opening turn of a new conversation with a customer we have history for.
    returning: bool = False


async def assemble(
    conversation_id: str,
    customer_ref: str,
    slots: dict[str, Any],
    consent: dict[str, Any] | None,
    turn_text: str,
) -> Recollection:
    """Build the context for one turn, within budget.

    Ordering is a priority statement: the profile and any recall are reserved
    first because they are small and dense, and the raw transcript — the least
    information per token — gets whatever is left.
    """
    settings = get_settings()
    used_tiers: list[str] = []

    # --- tier 2 first: small, exact, most valuable per token ----------------
    profile_block = profile.render(slots, consent)
    if profile_block:
        used_tiers.append("profile")

    # --- tier 3: only worth a lookup for a customer seen before -------------
    recall_block = ""
    if settings.enable_semantic_memory:
        recalled = await semantic.recall(
            customer_ref, turn_text, exclude_conversation=conversation_id
        )
        recall_block = semantic.render(recalled)
        if recall_block:
            used_tiers.append("semantic")

    # --- tier 1 gets whatever budget is left --------------------------------
    fixed_cost = tokens.estimate_tokens(profile_block) + tokens.estimate_tokens(recall_block)
    budget = max(0, settings.working_budget_tokens - fixed_cost)

    rows = await db.fetch_all(
        """
        SELECT direction, body FROM messages
        WHERE conversation_id = %s ORDER BY id DESC LIMIT %s
        """,
        conversation_id,
        settings.history_turns,
    )
    messages = [
        {"role": "customer" if r["direction"] == "in" else "agent", "text": r["body"]}
        for r in reversed(rows)
    ]
    history = tokens.fit_to_budget(messages, budget, text_of=lambda m: m["text"])
    if history:
        used_tiers.append("working")

    total = fixed_cost + sum(tokens.estimate_tokens(m["text"]) for m in history)

    if len(history) < len(messages):
        log.info(
            "history_trimmed",
            kept=len(history),
            available=len(messages),
            budget=budget,
            fixed_cost=fixed_cost,
        )

    return Recollection(
        returning=bool(recall_block) and len(messages) <= 1,
        history=history,
        profile_block=profile_block,
        recall_block=recall_block,
        tokens_used=total,
        tiers=used_tiers,
    )


__all__ = ["Recollection", "assemble", "conflict", "profile", "semantic", "tokens"]
