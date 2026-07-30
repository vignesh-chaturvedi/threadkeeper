"""What is tier 3 actually worth?

The plan asks for this explicitly: "run the eval suite with tier 3 off, report
the delta". The full eval harness is Phase 08, so this is the narrow version —
it measures the one thing tier 3 is supposed to buy, on the one scenario where
it could possibly help.

The scenario is a **returning customer**. They had a conversation months ago,
raised an objection, and left. They come back. Does having their prior summary
in the prompt change anything measurable?

    uv run python -m evals.memory_ab                 # fake provider, free
    TK_LLM_PROVIDER=gemini uv run python -m evals.memory_ab

Two metrics, chosen because they are checkable without a human:

  * **objection_recalled** — does the agent's reply reference the thing they
    complained about last time? That is the cross-sell motion tier 3 exists for.
  * **context_tokens** — what it costs, every turn, for every customer,
    including the ones who never came back.

The honest expectation, stated before running it: the second number is real and
the first is small. Reporting that is more useful than assuming retrieval is
essential — and it is the difference between "I used RAG" and "I measured RAG".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from typing import Any

from app import cache, db, memory
from app.graph.build import reset_graph
from app.graph.runner import run_turn
from app.ingress import repository
from app.logging import configure_logging
from app.privacy.refs import customer_ref
from app.settings import get_settings

# Each scenario: a first conversation that ends with an objection, then a
# return visit. `probe` is what we look for in the reply on the return visit.
SCENARIOS: list[dict[str, Any]] = [
    {
        "name": "processing-fees",
        "phone": "919900000001",
        "first": [
            "personal loan chahiye 5 lakh",
            "haan theek hai",
            "processing fees bahut zyada hain, nahi chahiye",
        ],
        "returns": "phir se dekhna hai loan ke baare mein",
        "probe": ["fee", "fees", "processing", "charge"],
    },
    {
        "name": "interest-rate",
        "phone": "919900000002",
        "first": [
            "home loan chahiye",
            "haan",
            "interest rate bahut zyada hai bhai, band karo",
        ],
        "returns": "loan ke liye baat karni thi",
        "probe": ["rate", "interest", "byaj"],
    },
    {
        "name": "no-objection-control",
        "phone": "919900000003",
        "first": ["business loan chahiye", "stop"],
        "returns": "loan chahiye",
        # Control: nothing to recall. Any "recall" here is a false positive.
        "probe": [],
    },
]


@dataclass
class Arm:
    label: str
    semantic_on: bool
    recalled: int = 0
    opportunities: int = 0
    false_positives: int = 0
    context_tokens: list[int] = field(default_factory=list)
    replies: list[dict[str, str]] = field(default_factory=list)

    @property
    def recall_rate(self) -> float:
        return self.recalled / self.opportunities if self.opportunities else 0.0

    @property
    def mean_context_tokens(self) -> float:
        return sum(self.context_tokens) / len(self.context_tokens) if self.context_tokens else 0.0


async def _wipe(phone: str) -> None:
    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", customer_ref(phone))


async def _say(cid: str, text: str) -> str:
    await db.execute(
        "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'in', %s)", cid, text
    )
    reply = await run_turn(cid, text)
    await db.execute(
        "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'out', %s)", cid, reply
    )
    return reply


async def run_arm(arm: Arm) -> Arm:
    settings = get_settings()
    object.__setattr__(settings, "enable_semantic_memory", arm.semantic_on)
    reset_graph()

    for scenario in SCENARIOS:
        phone, ref = scenario["phone"], customer_ref(scenario["phone"])
        await _wipe(phone)

        # --- visit one: they object and leave ---------------------------
        first = await repository.get_or_create_conversation("whatsapp", ref)
        first_id = str(first["id"])
        for text in scenario["first"]:
            await _say(first_id, text)

        # --- visit two: a fresh conversation, same customer -------------
        await db.execute(
            "UPDATE conversations SET customer_ref = %s WHERE id = %s", f"{ref}-old", first_id
        )
        await db.execute(
            "UPDATE conversation_summaries SET customer_ref = %s WHERE conversation_id = %s",
            ref,
            first_id,
        )
        second = await repository.get_or_create_conversation("whatsapp", ref)
        second_id = str(second["id"])

        recollection = await memory.assemble(second_id, ref, {}, {}, scenario["returns"])
        arm.context_tokens.append(recollection.tokens_used)

        reply = await _say(second_id, scenario["returns"])
        arm.replies.append({"scenario": scenario["name"], "reply": reply})

        probes = scenario["probe"]
        hit = any(p in reply.lower() for p in probes) if probes else False
        if probes:
            arm.opportunities += 1
            arm.recalled += int(hit)
        elif any(w in reply.lower() for w in ("last time", "previously", "pichli baar")):
            arm.false_positives += 1

        await _wipe(phone)
        await db.execute("DELETE FROM conversations WHERE customer_ref = %s", f"{ref}-old")

    return arm


async def main() -> int:
    parser = argparse.ArgumentParser(description="Measure what tier 3 memory is worth.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    configure_logging()
    await db.open_pool()
    await cache.open_redis()
    try:
        on = await run_arm(Arm("tier 3 ON", semantic_on=True))
        off = await run_arm(Arm("tier 3 OFF", semantic_on=False))

        report = {
            "provider": get_settings().llm_provider,
            "scenarios": len(SCENARIOS),
            "arms": {
                a.label: {
                    "objection_recall_rate": round(a.recall_rate, 3),
                    "recalled": a.recalled,
                    "opportunities": a.opportunities,
                    "false_positives": a.false_positives,
                    "mean_context_tokens": round(a.mean_context_tokens, 1),
                    "replies": a.replies,
                }
                for a in (on, off)
            },
            "delta": {
                "objection_recall_points": round((on.recall_rate - off.recall_rate) * 100, 1),
                "context_tokens_per_turn": round(
                    on.mean_context_tokens - off.mean_context_tokens, 1
                ),
            },
        }

        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"\n  memory A/B · provider={report['provider']} · {len(SCENARIOS)} scenarios\n")
            print(
                f"  {'arm':14} {'objection recall':>18} {'false pos':>11} {'ctx tokens/turn':>17}"
            )
            for a in (on, off):
                print(
                    f"  {a.label:14} {a.recalled}/{a.opportunities} "
                    f"({a.recall_rate * 100:.0f}%){'':>7} {a.false_positives:>10} "
                    f"{a.mean_context_tokens:>16.1f}"
                )
            d = report["delta"]
            print(
                f"\n  delta: {d['objection_recall_points']:+.0f} points of objection recall, "
                f"{d['context_tokens_per_turn']:+.1f} context tokens per turn"
            )
            print("\n  replies on the return visit (tier 3 ON):")
            for r in on.replies:
                print(f"    {r['scenario']:22} {r['reply'][:74]}")
            print()
        return 0
    finally:
        await cache.close_redis()
        await db.close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
