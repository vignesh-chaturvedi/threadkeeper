"""Compile the graph, once, against the Postgres checkpointer.

Durability lives in the checkpointer. Everything else here is wiring: extraction
runs first every turn, a conditional edge reads the decision extraction already
made, and each stage node terminates the turn.

The graph is intentionally shallow — one hop from `extract` to a stage node and
then out. It is not a ReAct loop and must not become one: a loop that can revisit
`consent` an unpredictable number of times is exactly the property that makes a
funnel unauditable.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, StateGraph

from app.db import pool
from app.graph.nodes import NODES, extract_slots, route
from app.graph.state import FunnelState
from app.logging import get_logger

log = get_logger(__name__)

_compiled: Any | None = None
# The pool the cached graph was compiled against. The checkpointer holds a
# reference to it, so if the pool is ever replaced — reconnect, test teardown,
# a restart in the same process — the cached graph is bound to a closed pool and
# every turn fails with PoolClosed. Cheaper to notice than to debug.
_compiled_for: int | None = None


def build() -> StateGraph:
    g = StateGraph(FunnelState)

    g.add_node("extract", extract_slots)
    for name, fn in NODES.items():
        g.add_node(name, fn)

    g.set_entry_point("extract")
    # The map is explicit rather than inferred, so an unreachable or misspelled
    # destination is a startup error instead of a runtime surprise.
    g.add_conditional_edges("extract", route, {name: name for name in NODES})

    for name in NODES:
        g.add_edge(name, END)

    return g


async def get_graph() -> Any:
    """Compiled once per pool, sharing the application's connection pool."""
    global _compiled, _compiled_for
    current = pool()
    if _compiled is None or _compiled_for != id(current):
        saver = AsyncPostgresSaver(conn=current)
        _compiled = build().compile(checkpointer=saver)
        _compiled_for = id(current)
        log.info("graph_compiled", nodes=len(NODES) + 1)
    return _compiled


def reset_graph() -> None:
    """Drop the cached graph. Used by tests simulating a full restart."""
    global _compiled, _compiled_for
    _compiled = None
    _compiled_for = None
