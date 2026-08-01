"""Graph nodes.

The shape every stage node shares: it writes prose and nothing else. It does not
decide where the conversation goes — `policy.decide` already did that, before
any node ran — and it does not decide what is true; `extract` did that.

Three responsibilities, three places. That separation is what makes the funnel
testable: the policy has unit tests with no model, extraction has a labelled set
(Phase 09), and the reply prompt can be rewritten without changing either.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.graph import prompts
from app.graph.policy import decide
from app.graph.state import FunnelState
from app.llm import ModelError, get_provider
from app.logging import get_logger
from app.memory import conflict
from app.settings import get_settings
from app.tools import client as tool_client

log = get_logger(__name__)

# Answering a direct question is stronger evidence than a value inferred from a
# passing remark, so the two are recorded with different provenance and the
# conflict rule arbitrates between them.
_CONFIRMED_AT_STAGE = {
    "consent": {"consent_granted"},
    "kyc_collect": {"pan_status"},
}


def _merge_slots(
    known: dict[str, Any],
    sources: dict[str, str],
    extracted: dict[str, Any],
    stage: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Apply the documented conflict rule, slot by slot.

    The rule itself lives in app/memory/conflict.py so it can be read and tested
    without a graph, a database or a model anywhere near it.
    """
    confirmed_here = _CONFIRMED_AT_STAGE.get(stage, set())
    now = datetime.now(UTC)

    merged = dict(known)
    merged_sources = dict(sources)

    for key, value in extracted.items():
        if value is None:
            continue
        incoming = conflict.SlotValue(
            value=value,
            source="confirmed" if key in confirmed_here else "extracted",
            updated_at=now,
        )
        existing = (
            conflict.SlotValue(value=known[key], source=sources.get(key, "extracted"))
            if key in known
            else None
        )
        outcome = conflict.resolve(key, existing, incoming)
        if outcome.changed:
            merged[key] = outcome.winner.value
            merged_sources[key] = outcome.winner.source
        elif outcome.reason != "unchanged":
            log.info("slot_conflict", key=key, kept=outcome.reason)

    return merged, merged_sources


def _add_usage(state: FunnelState, usage: Any) -> dict[str, int]:
    current = state.get("usage") or {"input_tokens": 0, "output_tokens": 0, "calls": 0}
    return {
        "input_tokens": current["input_tokens"] + usage.input_tokens,
        "output_tokens": current["output_tokens"] + usage.output_tokens,
        "calls": current["calls"] + usage.calls,
    }


# ---------------------------------------------------------------------------
# extract — runs first, every turn
# ---------------------------------------------------------------------------
async def extract_slots(state: FunnelState) -> dict[str, Any]:
    """One structured-output call. Facts only; it never writes prose.

    A failure here degrades to "learned nothing this turn" rather than failing
    the turn: the customer still gets an answer, just a less informed one.
    """
    provider = get_provider()
    stage = state.get("stage", "intent_route")
    known = state.get("slots") or {}

    gating = get_settings().stage_gating
    schema = prompts.PROMPT_GATED_SCHEMA if gating == "prompt" else prompts.EXTRACTION_SCHEMA

    try:
        result = await provider.extract(
            system=(
                prompts.EXTRACTION_SYSTEM_FEWSHOT
                if get_settings().extraction_strategy == "fewshot"
                else prompts.EXTRACTION_SYSTEM
            ),
            user=prompts.render_extraction_prompt(stage, known, state.get("turn_text", "")),
            schema=schema,
        )
        extracted, usage = result.data, result.usage
    except ModelError as exc:
        log.warning("extraction_failed", error=str(exc))
        extracted, usage = {}, type("U", (), {"input_tokens": 0, "output_tokens": 0, "calls": 0})()

    consent = dict(state.get("consent") or {})
    if "consent_granted" in extracted and stage == "consent":
        # Recorded with the exact wording shown, not just a boolean. "Customer
        # consented" is unfalsifiable without the text they saw.
        consent = {
            "granted": bool(extracted["consent_granted"]),
            "wording_hash": prompts.consent_wording_hash(),
            "at": datetime.now(UTC).isoformat(),
        }

    # `consent_granted` becomes a consent event, not a slot. `next_stage` is a
    # routing suggestion the prompt-gated variant returns — persisting it would
    # put "next_stage" in the customer's profile block, which is both wrong and
    # would quietly bias the A/B against the variant. `intent` describes this
    # message, not the customer: stored as a slot it would sit in the profile
    # block forever ("intent: opt_out" long after they came back) and be fought
    # over by a conflict rule built for durable facts. It rides on the state as
    # a per-turn field instead, which is also what Phase 10 groups drop-off by.
    _NOT_SLOTS = {"consent_granted", "next_stage", "intent"}
    slots, sources = _merge_slots(
        known,
        state.get("slot_sources") or {},
        {k: v for k, v in extracted.items() if k not in _NOT_SLOTS},
        stage,
    )

    patch: dict[str, Any] = {
        "slots": slots,
        "slot_sources": sources,
        "consent": consent,
        "intent": extracted.get("intent"),
        "interrupt": extracted.get("interrupt"),
        "escalate": extracted.get("interrupt") == "escalate",
        "usage": _add_usage(state, usage),
    }

    # The routing decision is computed here, as data, so it can be logged and
    # asserted on. The conditional edge below just reads it.
    decision = decide({**state, **patch})

    if gating == "prompt":
        # The variant under measurement: take the model's word for it, falling
        # back to the policy only when it names a stage that does not exist.
        # This is the whole of prompt-based gating — there is no third thing it
        # does that the deterministic version leaves out.
        suggested = extracted.get("next_stage")
        if suggested in NODES:
            patch["next_stage"] = suggested
            patch["route_reason"] = "model_suggested"
            patch["holds_stage"] = False
        else:
            patch["next_stage"] = decision.stage
            patch["route_reason"] = f"{decision.reason}_fallback"
            patch["holds_stage"] = decision.holds_stage
    else:
        patch["next_stage"] = decision.stage
        patch["route_reason"] = decision.reason
        patch["holds_stage"] = decision.holds_stage

    log.info(
        "extracted",
        learned=sorted(extracted.keys()),
        next_stage=decision.stage,
        reason=decision.reason,
    )
    return patch


def route(state: FunnelState) -> str:
    """The conditional edge. Reads a decision already made; makes none itself."""
    return state.get("next_stage", "qualify")


# ---------------------------------------------------------------------------
# stage nodes — all identical except for which guidance they render
# ---------------------------------------------------------------------------
async def _speak(
    state: FunnelState,
    stage: str,
    *,
    offers: list[dict[str, Any]] | None = None,
    offers_error: str | None = None,
) -> dict[str, Any]:
    provider = get_provider()

    try:
        result = await provider.reply(
            system=prompts.REPLY_SYSTEM,
            user=prompts.render_reply_prompt(
                stage,
                state.get("turn_text", ""),
                profile_block=state.get("profile_block", ""),
                recall_block=state.get("recall_block", ""),
                returning=bool(state.get("returning")),
                offers=offers,
                offers_error=offers_error,
            ),
            history=state.get("history", []),  # type: ignore[arg-type]
        )
        text, usage = result.text, result.usage
    except ModelError as exc:
        # Degrade rather than go silent. A customer who gets nothing assumes the
        # service is broken; a customer who gets this knows someone is coming.
        log.warning("reply_failed", stage=stage, error=str(exc))
        # Move the stage as well as the flag. Leaving stage where it was while
        # setting status='escalated' produces a conversation whose own record
        # disagrees with itself — and the funnel chart would then show a lead
        # sitting in `qualify` forever with nobody looking at it.
        return {
            "reply": (
                "Sorry — I'm having trouble right now. Let me get a colleague to pick this up."
            ),
            "stage": "escalate",
            "route_reason": "model_unavailable",
            "escalate": True,
        }

    # An interrupt answers the customer without moving the funnel on.
    resolved_stage = state.get("stage", stage) if state.get("holds_stage") else stage

    return {"reply": text, "stage": resolved_stage, "usage": _add_usage(state, usage)}


async def qualify(state: FunnelState) -> dict[str, Any]:
    return await _speak(state, "qualify")


async def consent(state: FunnelState) -> dict[str, Any]:
    """The one stage whose words are fixed rather than generated.

    Consent has to be specific and informed, and it has to be provable. A model
    paraphrasing it each time would make the wording hash meaningless and the
    ledger unauditable.
    """
    return {"reply": prompts.CONSENT_WORDING, "stage": "consent"}


async def kyc_collect(state: FunnelState) -> dict[str, Any]:
    return await _speak(state, "kyc_collect")


async def offer_match(state: FunnelState) -> dict[str, Any]:
    """The one stage that calls a lender before it speaks.

    Order matters: fetch first, then generate with the results in hand. A reply
    written before the tool returns can only describe offers it imagined, which
    is the exact failure Phase 08 scores as hard.
    """
    slots = state.get("slots") or {}
    conversation_id = state.get("conversation_id", "")

    result = await tool_client.invoke(
        "fetch_offers",
        {
            "product": slots.get("product") or "personal_loan",
            "income_band": slots.get("income_band") or "25k_50k",
            "city_tier": int(slots.get("city_tier") or 2),
            "amount_inr": int(slots.get("amount_inr") or 300_000),
            "conversation_id": conversation_id,
        },
        stage="offer_match",
        state=state,
        conversation_id=conversation_id,
    )

    if result.get("error"):
        # The lender is down or said no. Say so plainly rather than improvising
        # an offer — degrading gracefully is what the fault injection is for.
        log.info("offers_unavailable", reason=result.get("error"))
        patch = await _speak(state, "offer_match", offers=[], offers_error=result["error"])
        return patch

    offers = result.get("offers") or []
    patch = await _speak(state, "offer_match", offers=offers)
    # Kept in state for the escalation packet's `last_offer_shown`, and so
    # create_application can verify an offer was genuinely quoted.
    patch["last_offer"] = offers[0] if offers else None
    return patch


async def close(state: FunnelState) -> dict[str, Any]:
    return await _speak(state, "close")


async def escalate(state: FunnelState) -> dict[str, Any]:
    patch = await _speak(state, "escalate")
    patch["escalate"] = True
    return patch


async def handle_objection(state: FunnelState) -> dict[str, Any]:
    return await _speak(state, "handle_objection")


async def handle_off_topic(state: FunnelState) -> dict[str, Any]:
    return await _speak(state, "handle_off_topic")


NODES = {
    "qualify": qualify,
    "consent": consent,
    "kyc_collect": kyc_collect,
    "offer_match": offer_match,
    "close": close,
    "escalate": escalate,
    "handle_objection": handle_objection,
    "handle_off_topic": handle_off_topic,
}
