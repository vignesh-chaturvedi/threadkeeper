"""The questions the console answers, as SQL.

Kept apart from the rendering on purpose: every number on the page is a query
that can be run in psql, pasted into a ticket, or checked by someone who does not
trust the chart. A dashboard whose figures only exist inside a template is a
dashboard nobody can audit.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app import db
from app.graph.policy import STAGES

# The funnel, in the order a lead is meant to walk it. `intent_route` is the
# entry pseudo-stage every conversation starts in, so it is the denominator
# rather than a step.
FUNNEL_STAGES = [s for s in STAGES if s != "intent_route"]


async def funnel() -> list[dict[str, Any]]:
    """How many conversations ever reached each stage, and where they stopped.

    "Reached" means the stage appears anywhere in the conversation's transition
    history — not that it is the current stage. A lead that reached offers and
    then opted out still reached offers, and counting only current stages would
    show the funnel emptying itself over time.
    """
    rows = await db.fetch_all(
        """
        WITH reached AS (
          SELECT DISTINCT conversation_id, to_stage AS stage FROM stage_transitions
        ),
        totals AS (SELECT count(*) AS n FROM conversations)
        SELECT s.stage,
               count(r.conversation_id)                         AS reached,
               (SELECT n FROM totals)                           AS total
        FROM unnest(%s::text[]) WITH ORDINALITY AS s(stage, ord)
        LEFT JOIN reached r ON r.stage = s.stage
        GROUP BY s.stage, s.ord
        ORDER BY s.ord
        """,
        FUNNEL_STAGES,
    )

    out: list[dict[str, Any]] = []
    previous: int | None = None
    for row in rows:
        reached = int(row["reached"])
        total = int(row["total"]) or 1
        out.append(
            {
                "stage": row["stage"],
                "reached": reached,
                "total": int(row["total"]),
                "pct_of_total": round(reached / total * 100, 1),
                # Step conversion is the number that actually names the problem.
                # "40% reach offers" is a symptom; "only 55% of the leads who
                # gave consent get through KYC" is a place to look.
                "pct_of_previous": (
                    None
                    if previous is None
                    else round(reached / previous * 100, 1)
                    if previous
                    else 0.0
                ),
                "dropped_here": None if previous is None else max(0, previous - reached),
            }
        )
        previous = reached
    return out


async def drop_off_reasons(limit: int = 8) -> list[dict[str, Any]]:
    """Why conversations stopped where they stopped.

    Grouped by the *last* stage each conversation reached and the reason that
    put it there, which is the difference between "leads leave at KYC" and
    "leads leave at KYC after an objection".
    """
    return await db.fetch_all(
        """
        WITH last_turn AS (
          SELECT DISTINCT ON (conversation_id)
                 conversation_id, stage_out, reason, intent
          FROM turns
          ORDER BY conversation_id, turn_index DESC
        )
        SELECT stage_out AS stage, reason, intent, count(*) AS n
        FROM last_turn
        GROUP BY stage_out, reason, intent
        ORDER BY n DESC, stage_out
        LIMIT %s
        """,
        limit,
    )


async def unit_economics() -> dict[str, Any]:
    """Cost per conversation, and per closed sale.

    "Closed sale" is a conversation with an application row — the only
    definition this system can actually evidence. `conversations.status` has a
    'won' value that nothing sets, because nothing here hears back from a
    lender; reporting against it would be reporting on an empty column.

    Cost per sale is *total* spend over sales, not the spend on the winning
    conversations. The conversations that went nowhere are the cost of acquiring
    the ones that did.

    Every money figure is scoped to conversations that actually ran against a
    priced model. Most traffic in a dev database comes from the `fake` provider,
    which is priced at zero because it makes no call at all — averaging real
    spend across it does not produce a cheaper system, it produces a wrong
    number. The counts stay unscoped, and the page says which is which.
    """
    row = await db.fetch_one(
        """
        WITH priced AS (
          SELECT * FROM turns WHERE model <> 'fake'
        ),
        priced_conversations AS (
          SELECT DISTINCT conversation_id FROM priced
        )
        SELECT
          (SELECT count(*) FROM conversations)                      AS conversations,
          (SELECT count(*) FROM turns)                              AS turns,
          (SELECT count(*) FROM priced_conversations)               AS priced_conversations,
          (SELECT count(*) FROM priced)                             AS priced_turns,
          (SELECT coalesce(sum(cost_usd), 0) FROM priced)           AS total_usd,
          (SELECT coalesce(sum(tokens_in + tokens_out), 0) FROM priced) AS total_tokens,
          (SELECT count(DISTINCT conversation_id) FROM applications) AS sales,
          (SELECT count(DISTINCT a.conversation_id) FROM applications a
             JOIN priced_conversations p ON p.conversation_id = a.conversation_id)
                                                                    AS priced_sales,
          (SELECT count(*) FROM conversations WHERE status = 'escalated') AS escalated,
          (SELECT count(*) FROM turns WHERE degraded)               AS degraded_turns
        """
    )
    assert row is not None

    conversations = int(row["conversations"])
    priced_conversations = int(row["priced_conversations"])
    priced_turns = int(row["priced_turns"])
    priced_sales = int(row["priced_sales"])
    sales = int(row["sales"])
    total = Decimal(row["total_usd"])

    return {
        "conversations": conversations,
        "turns": int(row["turns"]),
        "priced_conversations": priced_conversations,
        "priced_turns": priced_turns,
        "total_usd": total,
        "total_tokens": int(row["total_tokens"]),
        "sales": sales,
        "escalated": int(row["escalated"]),
        "degraded_turns": int(row["degraded_turns"]),
        "usd_per_conversation": (
            (total / priced_conversations) if priced_conversations else Decimal(0)
        ),
        "usd_per_turn": (total / priced_turns) if priced_turns else Decimal(0),
        # None, not zero, and not a division by zero. "No sales yet" and "sales
        # are free" are different statements and the page must not confuse them.
        "usd_per_sale": (total / priced_sales) if priced_sales else None,
        "conversion_pct": round(sales / conversations * 100, 1) if conversations else 0.0,
    }


async def cost_by_stage() -> list[dict[str, Any]]:
    """Where the money goes. Spend is per turn, attributed to the stage it entered."""
    return await db.fetch_all(
        """
        SELECT stage_in AS stage,
               count(*)                        AS turns,
               coalesce(sum(cost_usd), 0)      AS usd,
               round(avg(latency_ms))          AS p50_ms,
               round(avg(tokens_in + tokens_out)) AS avg_tokens
        FROM turns
        GROUP BY stage_in
        ORDER BY usd DESC
        """
    )


async def recent_conversations(limit: int = 25) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        SELECT c.id, c.stage, c.status, c.created_at,
               count(t.id)                          AS turns,
               coalesce(sum(t.cost_usd), 0)         AS usd,
               max(t.at)                            AS last_turn_at,
               bool_or(a.id IS NOT NULL)            AS has_application
        FROM conversations c
        LEFT JOIN turns t ON t.conversation_id = c.id
        LEFT JOIN applications a ON a.conversation_id = c.id
        GROUP BY c.id
        ORDER BY coalesce(max(t.at), c.created_at) DESC
        LIMIT %s
        """,
        limit,
    )


async def conversation_trace(conversation_id: str) -> list[dict[str, Any]]:
    """Every turn of one conversation, with the tools each one called.

    The tool join is an aggregate rather than a row-per-tool join, so a turn that
    called two tools stays one turn. Tools are matched to a turn by time window,
    because `tool_calls` predates this table and carries no turn id — the
    alternative was a migration that back-fills a column nobody can back-fill
    correctly for rows already written.
    """
    return await db.fetch_all(
        """
        SELECT t.*,
               coalesce(
                 (SELECT json_agg(json_build_object(
                            'tool', tc.tool, 'latency_ms', tc.latency_ms,
                            'error', tc.error, 'denied_reason', tc.denied_reason)
                          ORDER BY tc.id)
                    FROM tool_calls tc
                   WHERE tc.conversation_id = t.conversation_id
                     AND tc.called_at <= t.at
                     AND tc.called_at > t.at - (t.latency_ms || ' milliseconds')::interval
                 ), '[]'::json) AS tools
        FROM turns t
        WHERE t.conversation_id = %s
        ORDER BY t.turn_index
        """,
        conversation_id,
    )


async def transcript(conversation_id: str) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        SELECT direction, body, received_at
        FROM messages
        WHERE conversation_id = %s
        ORDER BY received_at, id
        """,
        conversation_id,
    )


async def conversation_header(conversation_id: str) -> dict[str, Any] | None:
    return await db.fetch_one(
        """
        SELECT c.id, c.channel, c.stage, c.status, c.created_at, c.last_in_at,
               (SELECT coalesce(json_object_agg(s.key, s.value), '{}'::json)
                  FROM slots s WHERE s.conversation_id = c.id) AS slots,
               (SELECT count(*) FROM applications a WHERE a.conversation_id = c.id) AS applications
        FROM conversations c
        WHERE c.id = %s
        """,
        conversation_id,
    )
