"""Tier 3 — semantic recall over per-conversation summaries.

Two deliberate restrictions, and both are the point:

**One embedding per conversation, written at close.** Not per message. "hi",
"ok" and "haan" are the most frequent things anyone types and they carry no
information; a per-message index spends its top-k on greetings. This is the
approach I want to be able to say I tried and rejected with a number attached —
see `evals/memory_ab.py`.

**Scoped to one customer.** The question retrieval earns its keep on is "what
did *this* customer complain about last time", which is a cross-sell question.
"What do customers complain about" is analytics and belongs in SQL, not in a
vector index.

Everything here degrades to nothing on failure. A conversation that cannot be
summarised is a conversation without a summary, not a failed turn.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Json

from app import db
from app.llm import ModelError, get_provider
from app.logging import get_logger
from app.settings import get_settings

log = get_logger(__name__)

SUMMARY_SYSTEM = """\
You write a two-sentence internal note about a finished loan enquiry, for the \
next agent who talks to this customer months later.

Include: what they wanted, how far they got, and — most importantly — any \
objection or hesitation they voiced, in their own terms.
Exclude: greetings, pleasantries, and anything you are guessing at.
Never include a PAN, Aadhaar, phone number or account number.
Write plainly. No preamble, no bullet points."""


def _to_pgvector(vector: list[float]) -> str:
    """pgvector's text input format. psycopg has no native adapter for it here."""
    return "[" + ",".join(f"{v:.7f}" for v in vector) + "]"


async def summarise(conversation_id: str) -> tuple[str, list[str]]:
    """Write the note and pull out the objections. Returns ('', []) on failure."""
    rows = await db.fetch_all(
        """
        SELECT direction, body FROM messages
        WHERE conversation_id = %s ORDER BY id
        """,
        conversation_id,
    )
    if not rows:
        return "", []

    transcript = "\n".join(
        f"{'customer' if r['direction'] == 'in' else 'agent'}: {r['body']}" for r in rows
    )

    slot_rows = await db.fetch_all(
        "SELECT key, value FROM slots WHERE conversation_id = %s", conversation_id
    )
    slots = {r["key"]: r["value"] for r in slot_rows}
    objections = [str(slots["objection"])] if slots.get("objection") else []

    try:
        result = await get_provider().reply(system=SUMMARY_SYSTEM, user=transcript, history=[])
        return result.text, objections
    except ModelError as exc:
        log.warning("summary_failed", conversation_id=conversation_id, error=str(exc))
        return "", objections


async def store(conversation_id: str) -> int | None:
    """Summarise, embed and persist. Idempotent per conversation."""
    settings = get_settings()
    if not settings.enable_semantic_memory:
        return None

    conversation = await db.fetch_one(
        "SELECT customer_ref, channel, stage, status FROM conversations WHERE id = %s",
        conversation_id,
    )
    if conversation is None:
        return None

    summary, objections = await summarise(conversation_id)
    if not summary:
        return None

    vector: list[float] | None = None
    try:
        vector = (await get_provider().embed(text=summary)).vector
    except ModelError as exc:
        # A summary with no embedding is still worth keeping: it is readable by
        # a human in the escalation console even if it can never be retrieved.
        log.warning("embedding_failed", conversation_id=conversation_id, error=str(exc))

    row = await db.fetch_one(
        """
        INSERT INTO conversation_summaries
          (conversation_id, customer_ref, channel, summary, objections,
           outcome, final_stage, embedding)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (conversation_id) DO UPDATE
          SET summary = EXCLUDED.summary,
              objections = EXCLUDED.objections,
              outcome = EXCLUDED.outcome,
              final_stage = EXCLUDED.final_stage,
              embedding = EXCLUDED.embedding
        RETURNING id
        """,
        conversation_id,
        conversation["customer_ref"],
        conversation["channel"],
        summary,
        Json(objections),
        conversation["status"],
        conversation["stage"],
        _to_pgvector(vector) if vector else None,
    )
    log.info(
        "summary_stored",
        conversation_id=conversation_id,
        objections=len(objections),
        embedded=vector is not None,
    )
    return row["id"] if row else None


async def recall(
    customer_ref: str, query: str, *, exclude_conversation: str | None = None, k: int | None = None
) -> list[dict[str, Any]]:
    """Prior conversations with this customer, most relevant first.

    Falls back to plain recency when there is no embedding to compare against —
    which, with two or three prior conversations, is very nearly as good, and
    saying so out loud is more useful than pretending otherwise.
    """
    settings = get_settings()
    if not settings.enable_semantic_memory:
        return []
    k = k or settings.recall_top_k

    try:
        vector = (await get_provider().embed(text=query)).vector
    except ModelError as exc:
        log.warning("recall_embedding_failed", error=str(exc))
        vector = None

    if vector is None:
        rows = await db.fetch_all(
            """
            SELECT summary, objections, outcome, final_stage, created_at, NULL::float AS distance
            FROM conversation_summaries
            WHERE customer_ref = %s AND (%s::uuid IS NULL OR conversation_id <> %s::uuid)
            ORDER BY created_at DESC LIMIT %s
            """,
            customer_ref,
            exclude_conversation,
            exclude_conversation,
            k,
        )
    else:
        rows = await db.fetch_all(
            """
            SELECT summary, objections, outcome, final_stage, created_at,
                   embedding <=> %s::vector AS distance
            FROM conversation_summaries
            WHERE customer_ref = %s
              AND embedding IS NOT NULL
              AND (%s::uuid IS NULL OR conversation_id <> %s::uuid)
            ORDER BY distance LIMIT %s
            """,
            _to_pgvector(vector),
            customer_ref,
            exclude_conversation,
            exclude_conversation,
            k,
        )

    return [
        {
            "summary": r["summary"],
            "objections": r["objections"],
            "outcome": r["outcome"],
            "final_stage": r["final_stage"],
            "at": r["created_at"].isoformat(),
            "distance": float(r["distance"]) if r["distance"] is not None else None,
        }
        for r in rows
    ]


def render(recalled: list[dict[str, Any]]) -> str:
    """The recall block, as it appears in the prompt. Empty when nothing recalled."""
    if not recalled:
        return ""
    lines = ["Previous conversations with this customer:"]
    for item in recalled:
        objections = ", ".join(item["objections"]) if item["objections"] else None
        line = f"- {item['summary'].strip()}"
        if objections:
            line += f" (raised: {objections})"
        lines.append(line)
    return "\n".join(lines)
