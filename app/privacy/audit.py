"""The append-only audit log.

The question it exists to answer: *"why did the agent say that, in March?"* —
six months later, when the prompt has been rewritten twice and the model version
has changed.

So every entry carries the two things that make a reply reproducible rather than
merely recorded: the **prompt hash** and the **model**. Without those, a
transcript tells you what was said and nothing about why, and "we changed the
prompt at some point" is the end of the investigation.

Never raises. An audit write that can take down a turn is an audit write that
gets wrapped in a try/except by the next person to be paged, and then it is not
an audit log any more.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Json

from app import db
from app.logging import get_logger
from app.privacy import patterns

log = get_logger(__name__)


def _safe(detail: dict[str, Any]) -> dict[str, Any]:
    """Last line of defence. Values reaching here should already be tokenized."""
    out: dict[str, Any] = {}
    for key, value in detail.items():
        out[key] = patterns.scrub(value) if isinstance(value, str) else value
    return out


async def write(
    conversation_id: str | None,
    event: str,
    *,
    stage: str | None = None,
    actor: str = "agent",
    prompt_hash: str | None = None,
    model: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    try:
        await db.execute(
            """
            INSERT INTO audit_log
              (conversation_id, event, stage, actor, prompt_hash, model, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            conversation_id,
            event,
            stage,
            actor,
            prompt_hash,
            model,
            Json(_safe(detail or {})),
        )
    except Exception:
        log.exception("audit_write_failed", event=event)


async def trail(conversation_id: str, limit: int = 200) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        """
        SELECT event, stage, actor, prompt_hash, model, detail, at
        FROM audit_log WHERE conversation_id = %s ORDER BY id LIMIT %s
        """,
        conversation_id,
        limit,
    )
    return [
        {
            "event": r["event"],
            "stage": r["stage"],
            "actor": r["actor"],
            "prompt_hash": r["prompt_hash"],
            "model": r["model"],
            "detail": r["detail"],
            "at": r["at"].isoformat(),
        }
        for r in rows
    ]
