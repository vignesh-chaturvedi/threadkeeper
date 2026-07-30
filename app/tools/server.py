"""The MCP server.

    uv run python -m app.tools.server            # stdio, for Claude Desktop et al
    uv run python -m app.tools.server --http     # streamable HTTP

Writing a server rather than only consuming one is the point of this phase, and
it is a genuine server: any MCP client can connect, list these six tools and
call them. `tests/test_mcp_server.py` proves that by driving it over the
protocol rather than by calling the functions.

**Why the graph does not go through it.** The agent calls `client.invoke()`
in-process instead of speaking MCP to a sibling process. Routing every turn
through IPC would add a hop and a failure mode to reach code in the same
repository, for no product benefit. What matters is that both paths run the same
guard, the same idempotency and the same audit trail — there is one
implementation, with two doors. MCP is how *other* agents reach these tools;
in-process is how ours does.

Note for anyone comparing against the plan's sketch: it uses `FastMCP`, which
the Python SDK renamed to `MCPServer` in 2.0. Same decorator shape.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from mcp.server import MCPServer

from app import cache, db
from app.logging import configure_logging, get_logger
from app.tools import client, guard, registry

log = get_logger(__name__)

mcp = MCPServer(
    "threadkeeper-lender",
    instructions=(
        "Mock Indian lending marketplace. Every figure returned is computed from "
        "a fixed matrix, never sampled — so a number in a reply that did not come "
        "from these tools is a hallucination, and checkable as one. "
        "Tools are stage-scoped: pass the caller's current funnel stage."
    ),
)


# ---------------------------------------------------------------------------
# Registered explicitly rather than in a loop, so the signatures are visible to
# anyone reading the file — and so the MCP schema stays tight. A tool that takes
# **kwargs advertises "send me anything", which is the opposite of tool design.
# ---------------------------------------------------------------------------
@mcp.tool(description=registry.DESCRIPTIONS["check_eligibility"])
async def check_eligibility(
    product: str,
    income_band: str,
    city_tier: int = 2,
    amount_inr: int = 300_000,
    stage: str = "qualify",
    conversation_id: str | None = None,
) -> dict[str, Any]:
    return await client.invoke(
        "check_eligibility",
        {
            "product": product,
            "income_band": income_band,
            "city_tier": city_tier,
            "amount_inr": amount_inr,
        },
        stage=stage,
        state=await registry._state_for(conversation_id) if conversation_id else {},
        conversation_id=conversation_id,
    )


@mcp.tool(description=registry.DESCRIPTIONS["fetch_offers"])
async def fetch_offers(
    product: str,
    income_band: str,
    conversation_id: str,
    city_tier: int = 2,
    amount_inr: int = 300_000,
    stage: str = "offer_match",
) -> dict[str, Any]:
    return await client.invoke(
        "fetch_offers",
        {
            "product": product,
            "income_band": income_band,
            "city_tier": city_tier,
            "amount_inr": amount_inr,
            "conversation_id": conversation_id,
        },
        stage=stage,
        state=await registry._state_for(conversation_id),
        conversation_id=conversation_id,
    )


@mcp.tool(description=registry.DESCRIPTIONS["verify_pan"])
async def verify_pan(
    pan: str, conversation_id: str | None = None, stage: str = "kyc_collect"
) -> dict[str, Any]:
    return await client.invoke(
        "verify_pan",
        {"pan": pan},
        stage=stage,
        state=await registry._state_for(conversation_id) if conversation_id else {},
        conversation_id=conversation_id,
    )


@mcp.tool(description=registry.DESCRIPTIONS["create_application"])
async def create_application(
    conversation_id: str,
    offer_id: str,
    consent_ref: str,
    idem_key: str,
    stage: str = "close",
) -> dict[str, Any]:
    """Requires a consent_ref. Enforced here, not in the prompt."""
    return await client.invoke(
        "create_application",
        {
            "conversation_id": conversation_id,
            "offer_id": offer_id,
            "consent_ref": consent_ref,
            "idem_key": idem_key,
        },
        stage=stage,
        state=await registry._state_for(conversation_id),
        conversation_id=conversation_id,
    )


@mcp.tool(description=registry.DESCRIPTIONS["schedule_followup"])
async def schedule_followup(
    conversation_id: str,
    delay_hours: float = 24.0,
    reason: str = "no_reply",
    stage_at_drop: str = "unknown",
    stage: str = "close",
) -> dict[str, Any]:
    return await client.invoke(
        "schedule_followup",
        {
            "conversation_id": conversation_id,
            "delay_hours": delay_hours,
            "reason": reason,
            "stage_at_drop": stage_at_drop,
        },
        stage=stage,
        state=await registry._state_for(conversation_id),
        conversation_id=conversation_id,
    )


@mcp.tool(description=registry.DESCRIPTIONS["escalate_to_human"])
async def escalate_to_human(
    conversation_id: str, reason: str = "agent_requested", stage: str = "escalate"
) -> dict[str, Any]:
    return await client.invoke(
        "escalate_to_human",
        {"conversation_id": conversation_id, "reason": reason},
        stage=stage,
        state=await registry._state_for(conversation_id),
        conversation_id=conversation_id,
    )


TOOL_NAMES = tuple(registry.TOOLS)


async def _serve(http: bool) -> None:
    configure_logging()
    await db.open_pool()
    await cache.open_redis()
    log.info(
        "mcp_server_starting",
        transport="http" if http else "stdio",
        tools=list(TOOL_NAMES),
        stage_scoped=len(guard.ALLOWED),
    )
    try:
        if http:
            await mcp.run_streamable_http_async()
        else:
            await mcp.run_stdio_async()
    finally:
        await cache.close_redis()
        await db.close_pool()


def main() -> int:
    parser = argparse.ArgumentParser(description="Threadkeeper's MCP lender tool server.")
    parser.add_argument(
        "--http", action="store_true", help="serve streamable HTTP instead of stdio"
    )
    args = parser.parse_args()
    asyncio.run(_serve(args.http))
    return 0


if __name__ == "__main__":
    sys.exit(main())
