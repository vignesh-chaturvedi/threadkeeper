"""Time-travel replay: rerun a stored conversation against the current graph.

The question this answers is "did my change to the routing rules break turn 7 of
a conversation that used to work?" — which is otherwise unanswerable without
re-eliciting the same messages from a human.

How it works: take the customer's inbound messages from a real conversation, in
order, and replay them into a *shadow* conversation with its own checkpoint
thread. Nothing about the original is touched and nothing is sent to anyone. Then
diff the stage path and the final slots.

This is not the eval harness (Phase 08). Evals ask "is the agent good?" against
simulated customers; replay asks "is the agent still doing what it used to?"
against real traffic. Regression versus quality.

    uv run python -m app.graph.replay <conversation_id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from app import cache, db
from app.graph.build import get_graph
from app.graph.runner import run_turn
from app.logging import configure_logging, get_logger
from app.privacy.refs import customer_ref

log = get_logger(__name__)

SHADOW_PREFIX = "replay:"


async def _inbound_messages(conversation_id: str) -> list[str]:
    rows = await db.fetch_all(
        """
        SELECT body FROM messages
        WHERE conversation_id = %s AND direction = 'in'
        ORDER BY id
        """,
        conversation_id,
    )
    return [r["body"] for r in rows]


async def _stage_path(conversation_id: str) -> list[str]:
    rows = await db.fetch_all(
        """
        SELECT to_stage FROM stage_transitions
        WHERE conversation_id = %s ORDER BY id
        """,
        conversation_id,
    )
    return [r["to_stage"] for r in rows]


# Fields that legitimately differ on every run. Comparing them would make the
# replay report "changed" for every conversation, which is the fastest way to
# make a regression tool ignored.
_VOLATILE = {"at", "updated_at", "timestamp", "created_at"}


def _comparable(value: Any) -> Any:
    """Strip wall-clock fields so a diff shows behaviour changes, not clock ticks."""
    if isinstance(value, dict):
        return {k: _comparable(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        return [_comparable(v) for v in value]
    return value


async def _slots(conversation_id: str) -> dict[str, Any]:
    rows = await db.fetch_all(
        "SELECT key, value FROM slots WHERE conversation_id = %s ORDER BY key",
        conversation_id,
    )
    return {r["key"]: r["value"] for r in rows}


async def _make_shadow(source_id: str) -> str:
    """A throwaway conversation the replay can safely mutate."""
    ref = customer_ref(f"{SHADOW_PREFIX}{source_id}")
    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)
    row = await db.fetch_one(
        """
        INSERT INTO conversations (channel, customer_ref)
        VALUES ('replay', %s)
        RETURNING id
        """,
        ref,
    )
    return str(row["id"])


async def replay(source_id: str, *, keep_shadow: bool = False) -> dict[str, Any]:
    messages = await _inbound_messages(source_id)
    if not messages:
        raise SystemExit(f"conversation {source_id} has no inbound messages")

    original_path = await _stage_path(source_id)
    original_slots = await _slots(source_id)

    shadow_id = await _make_shadow(source_id)
    graph = await get_graph()

    replayed: list[dict[str, str]] = []
    for i, text in enumerate(messages, start=1):
        # The shadow conversation needs the customer's message on record too,
        # because the reply call reads history from the messages table.
        await db.execute(
            """
            INSERT INTO messages (conversation_id, direction, body)
            VALUES (%s, 'in', %s)
            """,
            shadow_id,
            text,
        )
        reply = await run_turn(shadow_id, text)
        await db.execute(
            "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'out', %s)",
            shadow_id,
            reply,
        )
        state = await graph.aget_state({"configurable": {"thread_id": shadow_id}})
        replayed.append(
            {
                "turn": str(i),
                "customer": text,
                "stage": state.values.get("stage", "?"),
                "reason": state.values.get("route_reason", "?"),
                "agent": reply,
            }
        )

    new_path = await _stage_path(shadow_id)
    new_slots = await _slots(shadow_id)

    report = {
        "source_conversation": source_id,
        "shadow_conversation": shadow_id,
        "turns": len(messages),
        "stage_path": {
            "original": original_path,
            "replayed": new_path,
            "identical": original_path == new_path,
        },
        "slots": {
            "original": original_slots,
            "replayed": new_slots,
            "changed": sorted(
                k
                for k in set(original_slots) | set(new_slots)
                if _comparable(original_slots.get(k)) != _comparable(new_slots.get(k))
            ),
        },
        "transcript": replayed,
    }

    if not keep_shadow:
        await db.execute("DELETE FROM conversations WHERE id = %s", shadow_id)

    return report


def _print(report: dict[str, Any]) -> None:
    path = report["stage_path"]
    mark = "IDENTICAL" if path["identical"] else "CHANGED"
    print(f"\nreplay of {report['source_conversation']} — {report['turns']} turns\n")
    for t in report["transcript"]:
        print(f"  {t['turn']:>2}. customer: {t['customer'][:66]}")
        print(f"      → [{t['stage']}] ({t['reason']}) {t['agent'][:66]}")
    print(f"\n  stage path  {mark}")
    print(f"    original: {' → '.join(path['original']) or '(none)'}")
    print(f"    replayed: {' → '.join(path['replayed']) or '(none)'}")
    if report["slots"]["changed"]:
        print(f"\n  slots that differ: {', '.join(report['slots']['changed'])}")
    else:
        print("\n  slots identical")
    print()


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Replay a conversation against the current graph.")
    parser.add_argument("conversation_id")
    parser.add_argument("--json", action="store_true", help="emit the full report as JSON")
    parser.add_argument("--keep", action="store_true", help="do not delete the shadow conversation")
    args = parser.parse_args()

    configure_logging()
    await db.open_pool()
    await cache.open_redis()
    try:
        report = await replay(args.conversation_id, keep_shadow=args.keep)
        if args.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            _print(report)
        return 0 if report["stage_path"]["identical"] else 1
    finally:
        await cache.close_redis()
        await db.close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
