"""Run N simulated conversations and score them.

    uv run python -m evals.runner                    # fake provider, free, deterministic
    uv run python -m evals.runner --repeat 10        # 50 conversations
    TK_LLM_PROVIDER=gemini uv run python -m evals.runner --json

Every run writes its transcripts to `evals/artifacts/` — gitignored, because a
transcript is a conversation and this repo does not commit those, but present on
disk so a failure can be read rather than guessed at.

Exits non-zero on any hard failure. That is what makes it usable as a gate:
`evals` in CI either passes or the PR does not merge.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import cache, db
from app.graph.build import reset_graph
from app.graph.runner import run_turn
from app.ingress import repository
from app.logging import configure_logging, get_logger
from app.privacy.refs import customer_ref
from app.scheduler import queue
from app.settings import get_settings
from evals import personas as persona_lib
from evals import scorecard

log = get_logger(__name__)

ARTIFACTS = Path(__file__).parent / "artifacts"


async def _fresh_conversation(persona: str, run: int) -> str:
    """A conversation nobody else is using, wiped of prior state."""
    ref = customer_ref(f"eval:{persona}:{run}")
    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)
    conv = await repository.get_or_create_conversation("whatsapp", ref)
    cid = str(conv["id"])
    for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
        await db.execute(f"DELETE FROM {table} WHERE thread_id = %s", cid)  # noqa: S608
    return cid


async def simulate(
    persona: persona_lib.Persona, run: int, max_turns: int | None = None
) -> tuple[scorecard.Score, list[tuple[str, str]]]:
    """One conversation, end to end, scored."""
    cid = await _fresh_conversation(persona.name, run)
    transcript: list[tuple[str, str]] = []
    cap = max_turns or persona.max_turns

    for _ in range(cap):
        message = await persona_lib.next_message(persona, transcript)
        if message is None:
            # The ghoster just stops. So does anyone who has finished.
            break

        await db.execute(
            "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'in', %s)",
            cid,
            message,
        )
        reply = await run_turn(cid, message)
        await db.execute(
            "INSERT INTO messages (conversation_id, direction, body) VALUES (%s, 'out', %s)",
            cid,
            reply,
        )
        transcript += [("customer", message), ("agent", reply)]

    score = await scorecard.score(persona.name, cid, transcript, persona.expects)
    return score, transcript


async def run_suite(repeat: int = 1, only: str | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    settings = get_settings()
    chosen = [p for p in persona_lib.load_all() if only is None or p.name == only]
    if not chosen:
        raise SystemExit(f"no persona matching {only!r}")

    scores: list[scorecard.Score] = []
    transcripts: dict[str, list[tuple[str, str]]] = {}
    created: list[str] = []

    for run in range(repeat):
        for persona in chosen:
            reset_graph()
            score, transcript = await simulate(persona, run)
            created.append(score.conversation_id)
            scores.append(score)
            transcripts[f"{persona.name}#{run}"] = transcript
            log.info(
                "eval_conversation",
                persona=persona.name,
                run=run,
                turns=score.turns,
                consent=score.reached_consent,
                kyc=score.kyc_complete,
                hard_failure=score.hard_failure,
            )

    summary = scorecard.summarise(scores)
    report = {
        "at": datetime.now(UTC).isoformat(),
        "provider": settings.llm_provider,
        "model": settings.gemini_reply_model if settings.llm_provider == "gemini" else "fake",
        "stage_gating": settings.stage_gating,
        "repeat": repeat,
        "elapsed_s": round(time.perf_counter() - started, 1),
        "summary": summary.as_dict(),
        "scores": [s.as_dict() for s in scores],
    }
    # Eval conversations arm follow-ups like any other. Leaving them queued
    # would have the worker nudge simulated customers forever — and scoped to
    # *these* ids, because cancelling every pending nudge would silently break
    # real conversations sharing the database.
    if created:
        await db.execute(
            "UPDATE followups SET status = 'cancelled', cancelled_reason = 'eval_cleanup', "
            "updated_at = now() WHERE status IN ('pending','running') "
            "AND conversation_id = ANY(%s::uuid[])",
            created,
        )
        await queue.reconcile()

    _write_artifacts(report, transcripts)
    return report


def _write_artifacts(report: dict[str, Any], transcripts: dict[str, list[tuple[str, str]]]) -> None:
    """Transcripts on disk, so a failure can be read rather than guessed at."""
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_dir = ARTIFACTS / f"{stamp}-{report['provider']}-{report['stage_gating']}"
    run_dir.mkdir(exist_ok=True)

    (run_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    for name, transcript in transcripts.items():
        lines = [
            f"{'customer' if who == 'customer' else '   agent'}: {text}" for who, text in transcript
        ]
        (run_dir / f"{name.replace('#', '-')}.txt").write_text("\n".join(lines), encoding="utf-8")

    latest = ARTIFACTS / "latest.json"
    latest.write_text(json.dumps(report, indent=2), encoding="utf-8")


def print_report(report: dict[str, Any]) -> None:
    s = report["summary"]
    print(
        f"\n  eval · {report['provider']}/{report['model']} · gating={report['stage_gating']} "
        f"· {s['runs']} conversations · {report['elapsed_s']}s\n"
    )
    print(
        f"  {'persona':22} {'turns':>5} {'consent':>8} {'kyc':>5} {'offers':>7} "
        f"{'tools':>6} {'$':>9}  flags"
    )
    for row in report["scores"]:
        flags = []
        if row["hallucinated_rate"]:
            flags.append(f"HALLUCINATED {','.join(row['invented_numbers'][:3])}")
        if row["off_policy_promise"]:
            flags.append("PROMISE")
        if not row["expectations_met"]:
            flags.append("expectations: " + "; ".join(row["expectation_failures"]))
        print(
            f"  {row['persona']:22} {row['turns']:>5} {row['reached_consent']!s:>8} "
            f"{row['kyc_complete']!s:>5} {row['reached_offers']!s:>7} "
            f"{row['tool_calls']:>6} {row['usd_cost']:>9.5f}  {' | '.join(flags)}"
        )

    print(f"\n  consent rate          {s['consent_rate'] * 100:5.1f}%")
    print(f"  KYC completion        {s['kyc_completion_rate'] * 100:5.1f}%")
    print(f"  reached offers        {s['offers_rate'] * 100:5.1f}%")
    print(f"  hallucinated rates    {s['hallucinated_rates']:5d}   (hard failure)")
    print(f"  off-policy promises   {s['off_policy_promises']:5d}   (hard failure)")
    print(f"  mean turns            {s['mean_turns']:5.1f}")
    if s["mean_turns_to_close"] is not None:
        print(f"  mean turns to close   {s['mean_turns_to_close']:5.1f}")
    print(f"  expectations met      {s['expectations_met']:5d} / {s['runs']}")
    print(
        f"  cost                  ${s['total_usd']:.5f} total, "
        f"${s['usd_per_conversation']:.5f} per conversation\n"
    )


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Run the persona eval suite.")
    parser.add_argument("--repeat", type=int, default=1, help="runs per persona")
    parser.add_argument("--only", help="run a single persona by name")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    configure_logging()
    await db.open_pool()
    await cache.open_redis()
    try:
        report = await run_suite(repeat=args.repeat, only=args.only)
        print(json.dumps(report, indent=2) if args.json else "", end="")
        if not args.json:
            print_report(report)
        # Non-zero on any hard failure: this is a gate, not a dashboard.
        return 1 if report["summary"]["hard_failures"] else 0
    finally:
        await cache.close_redis()
        await db.close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
