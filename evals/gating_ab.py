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
import math
import sys
from typing import Any

from app import cache, db
from app.logging import configure_logging
from app.settings import get_settings
from evals import runner

# Two-sided 95%, and 80% power. Named rather than inlined so the choice is
# visible: these are conventions, not results.
Z_ALPHA = 1.959964
Z_POWER = 0.841621


def _wilson(successes: float, n: int, z: float = Z_ALPHA) -> tuple[float, float]:
    """Wilson score interval.

    Not the textbook normal approximation: at n=25 with a rate near 0 or 1 that
    one produces bounds outside [0,1] and is simply wrong in exactly the region
    these runs land in.
    """
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _newcombe(p_a: float, n_a: int, p_b: float, n_b: int) -> tuple[float, float]:
    """95% CI on (b - a) for two independent proportions.

    Newcombe's hybrid score method, which composes the two Wilson intervals
    rather than assuming normality of the difference — the same reason as above,
    and it behaves when one arm hits 0% or 100%.
    """
    l_a, u_a = _wilson(p_a * n_a, n_a)
    l_b, u_b = _wilson(p_b * n_b, n_b)
    diff = p_b - p_a
    lower = diff - math.sqrt((p_b - l_b) ** 2 + (u_a - p_a) ** 2)
    upper = diff + math.sqrt((u_b - p_b) ** 2 + (p_a - l_a) ** 2)
    return (lower, upper)


def _n_for_power(p_a: float, p_b: float) -> int | None:
    """Conversations per arm needed to call the *observed* effect, at 80% power.

    The number that turns "inconclusive" from a shrug into a plan. If the honest
    answer is that this experiment needed 300 conversations per arm and the free
    tier affords 25, that is a finding about the experiment worth stating.
    """
    delta = abs(p_b - p_a)
    if delta < 1e-9:
        return None
    var = p_a * (1 - p_a) + p_b * (1 - p_b)
    return math.ceil((Z_ALPHA + Z_POWER) ** 2 * var / delta**2)


def _verdict(metric: str, p_a: float, n_a: int, p_b: float, n_b: int) -> dict[str, Any]:
    """One metric, compared honestly: effect, interval, and whether it lands."""
    lower, upper = _newcombe(p_a, n_a, p_b, n_b)
    significant = lower > 0 or upper < 0
    return {
        "metric": metric,
        "prompt_gated": round(p_a, 4),
        "code_gated": round(p_b, 4),
        "delta_pts": round((p_b - p_a) * 100, 1),
        "ci95_pts": [round(lower * 100, 1), round(upper * 100, 1)],
        "significant": significant,
        "n_per_arm_for_80pct_power": None if significant else _n_for_power(p_a, p_b),
    }


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
        n_p, n_c = int(p["runs"]), int(c["runs"])
        verdicts = [
            _verdict("consent rate", p["consent_rate"], n_p, c["consent_rate"], n_c),
            _verdict(
                "KYC completion", p["kyc_completion_rate"], n_p, c["kyc_completion_rate"], n_c
            ),
            _verdict("reached offers", p["offers_rate"], n_p, c["offers_rate"], n_c),
        ]
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
            "verdicts": verdicts,
        }

        # The comparison is the artifact, not the stdout it scrolled past. A run
        # that costs half a day's quota should not have to be repeated because
        # nobody redirected it to a file.
        runner.ARTIFACTS.mkdir(parents=True, exist_ok=True)
        out = runner.ARTIFACTS / "gating_ab.json"
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")

        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"\n  written to {out}")
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

            # The part that decides whether any of the above means anything.
            print(f"  {'metric':26} {'delta':>9} {'95% CI':>18}   verdict")
            for v in verdicts:
                lo, hi = v["ci95_pts"]
                if v["significant"]:
                    verdict = "significant"
                else:
                    need = v["n_per_arm_for_80pct_power"]
                    verdict = (
                        "inconclusive" if need is None else f"inconclusive · needs n≈{need}/arm"
                    )
                print(
                    f"  {v['metric']:26} {v['delta_pts']:+8.1f}p "
                    f"{f'[{lo:+.1f}, {hi:+.1f}]':>18}   {verdict}"
                )
            print(
                "\n  A CI straddling zero means this run cannot tell the arms apart —\n"
                "  which is a result, not a missing one.\n"
            )
        return 0
    finally:
        await cache.close_redis()
        await db.close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
