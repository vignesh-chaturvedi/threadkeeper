"""Code-gated versus prompt-gated routing, measured.

The plan's strongest suggested line is a before/after on exactly one change:
moving stage gating out of the prompt. This runs the eval suite twice — once
with `policy.decide()` in charge, once with the model asked where to go next —
and reports the difference.

    uv run python -m evals.gating_ab                        # fake, free
    TK_LLM_PROVIDER=gemini uv run python -m evals.gating_ab --repeat 4

Whatever number comes out is the number reported. The point of building the
prompt-gated variant properly, rather than a strawman, is that the comparison
has to be capable of embarrassing the thesis — otherwise it is decoration.

What it actually measures, beyond completion:

  * **out-of-order consent** — conversations that reached KYC or offers without
    consent ever having been granted. In a regulated flow this is not a quality
    metric, it is an incident count.
  * **hard failures** — invented figures and approval promises.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from app import cache, db
from app.logging import configure_logging
from app.settings import get_settings
from evals import runner


async def _out_of_order_consent(conversation_ids: list[str]) -> int:
    """Conversations that reached KYC or offers with no consent on record.

    The failure prompt-gating produces that a completion rate alone will not
    show: the funnel finished faster because it skipped the gate.
    """
    if not conversation_ids:
        return 0
    row = await db.fetch_one(
        """
        SELECT count(*) AS n FROM (
          SELECT t.conversation_id
          FROM stage_transitions t
          WHERE t.conversation_id = ANY(%s::uuid[])
            AND t.to_stage IN ('kyc_collect', 'offer_match')
          GROUP BY t.conversation_id
          HAVING NOT EXISTS (
            SELECT 1 FROM consent_ledger c
            WHERE c.conversation_id = t.conversation_id AND c.event = 'granted'
          )
        ) offenders
        """,
        conversation_ids,
    )
    return int(row["n"]) if row else 0


async def run_arm(gating: str, repeat: int) -> dict[str, Any]:
    settings = get_settings()
    object.__setattr__(settings, "stage_gating", gating)

    report = await runner.run_suite(repeat=repeat)
    ids = [s["conversation_id"] for s in report["scores"]]
    report["summary"]["out_of_order_consent"] = await _out_of_order_consent(ids)
    return report


def _delta(a: float, b: float) -> str:
    return f"{(a - b) * 100:+.1f}pts"


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Measure code- vs prompt-gated routing.")
    parser.add_argument("--repeat", type=int, default=2, help="runs per persona, per arm")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    configure_logging()
    await db.open_pool()
    await cache.open_redis()
    try:
        prompt_gated = await run_arm("prompt", args.repeat)
        code_gated = await run_arm("code", args.repeat)

        p, c = prompt_gated["summary"], code_gated["summary"]
        report = {
            "provider": get_settings().llm_provider,
            "model": code_gated["model"],
            "conversations_per_arm": p["runs"],
            "prompt_gated": p,
            "code_gated": c,
            "delta": {
                "consent_rate_pts": round((c["consent_rate"] - p["consent_rate"]) * 100, 1),
                "kyc_completion_pts": round(
                    (c["kyc_completion_rate"] - p["kyc_completion_rate"]) * 100, 1
                ),
                "out_of_order_consent": c["out_of_order_consent"] - p["out_of_order_consent"],
                "hard_failures": c["hard_failures"] - p["hard_failures"],
            },
        }

        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(
                f"\n  stage gating A/B · {report['provider']}/{report['model']} "
                f"· {p['runs']} conversations per arm\n"
            )
            print(f"  {'metric':26} {'prompt-gated':>13} {'code-gated':>12} {'delta':>10}")
            print(
                f"  {'consent rate':26} {p['consent_rate'] * 100:12.1f}% "
                f"{c['consent_rate'] * 100:11.1f}% "
                f"{_delta(c['consent_rate'], p['consent_rate']):>10}"
            )
            print(
                f"  {'KYC completion':26} {p['kyc_completion_rate'] * 100:12.1f}% "
                f"{c['kyc_completion_rate'] * 100:11.1f}% "
                f"{_delta(c['kyc_completion_rate'], p['kyc_completion_rate']):>10}"
            )
            print(
                f"  {'reached offers':26} {p['offers_rate'] * 100:12.1f}% "
                f"{c['offers_rate'] * 100:11.1f}% "
                f"{_delta(c['offers_rate'], p['offers_rate']):>10}"
            )
            print(
                f"  {'OUT-OF-ORDER CONSENT':26} {p['out_of_order_consent']:13d} "
                f"{c['out_of_order_consent']:12d} "
                f"{c['out_of_order_consent'] - p['out_of_order_consent']:+10d}"
            )
            print(
                f"  {'hard failures':26} {p['hard_failures']:13d} {c['hard_failures']:12d} "
                f"{c['hard_failures'] - p['hard_failures']:+10d}"
            )
            print(
                f"  {'expectations met':26} {p['expectations_met']:13d} {c['expectations_met']:12d}"
            )
            print(
                f"  {'cost per conversation':26} ${p['usd_per_conversation']:12.5f} "
                f"${c['usd_per_conversation']:11.5f}\n"
            )
        return 0
    finally:
        await cache.close_redis()
        await db.close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
