"""Score extraction against the hand-labelled set.

    uv run python -m evals.intent_f1                          # fake, free
    TK_LLM_PROVIDER=gemini uv run python -m evals.intent_f1
    TK_LLM_PROVIDER=gemini uv run python -m evals.intent_f1 --compare

A number in a README beats an adjective, and this is the measurement that
produces one. It is also a much tighter experiment than the Phase 08 A/B: no
model plays the customer, the labels are fixed, and the unit is a message rather
than a whole funnel — so 150 samples say considerably more here than 10
conversations did there.

Two metrics, and they answer different questions:

  * **intent accuracy** — did it understand what the customer was doing? One
    label per message, so plain accuracy is the honest summary.
  * **slot F1** — did it extract the right facts? Precision and recall are
    separately interesting: a low-recall extractor asks the customer things they
    already answered, and a low-precision one silently believes wrong facts.
    Precision matters more here, because a wrong income band routes a real
    person to the wrong lender.

Everything is broken down by script, because "handles Devanagari" is a claim
that needs a number rather than a mention.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import cache, db
from app.graph import prompts
from app.llm import ModelError, get_provider
from app.logging import configure_logging, get_logger
from app.settings import get_settings

log = get_logger(__name__)

LABELLED_SET = Path(__file__).parent / "intent_set.jsonl"
ARTIFACTS = Path(__file__).parent / "artifacts"

# The slots the labelled set actually annotates. Scoring a field nobody labelled
# would report a precision of zero for a question never asked.
SCORED_SLOTS = (
    "product",
    "amount_inr",
    "income_band",
    "pan_status",
    "consent_granted",
    "opted_out",
    "objection",
)

# `objection` is a free-text label in both the gold set and the model output —
# "interest_rate" vs "rate" vs "high interest" all mean the same thing. Scoring
# it on exact string equality would measure vocabulary, not understanding.
FUZZY_SLOTS = {"objection"}


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def support(self) -> int:
        return self.tp + self.fn


@dataclass
class Result:
    strategy: str
    provider: str
    model: str
    total: int = 0
    intent_correct: int = 0
    intent_by_script: dict[str, list[int]] = field(
        default_factory=lambda: defaultdict(lambda: [0, 0])
    )
    slots: dict[str, Counts] = field(default_factory=lambda: defaultdict(Counts))
    slots_by_script: dict[str, Counts] = field(default_factory=lambda: defaultdict(Counts))
    confusions: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    errors: int = 0
    misses: list[dict[str, Any]] = field(default_factory=list)

    @property
    def intent_accuracy(self) -> float:
        return self.intent_correct / self.total if self.total else 0.0

    @property
    def micro(self) -> Counts:
        total = Counts()
        for counts in self.slots.values():
            total.tp += counts.tp
            total.fp += counts.fp
            total.fn += counts.fn
        return total


def load_set(limit: int | None = None) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in LABELLED_SET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return rows[:limit] if limit else rows


def _same(slot: str, gold: Any, got: Any) -> bool:
    """Equality, with one deliberate loosening for free-text labels."""
    if slot in FUZZY_SLOTS:
        # Token overlap after a crude stem, because "fees" and "processing_fee"
        # are the same objection and a metric that disagrees is measuring
        # plurals. Stemming is one rule — drop a trailing "s" — which is enough
        # for a seven-value label space and does not pretend to be a stemmer.
        def stems(value: Any) -> set[str]:
            words = str(value).lower().replace("_", " ").replace("-", " ").split()
            return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words}

        return bool(stems(gold) & stems(got))
    if slot == "amount_inr":
        try:
            return int(gold) == int(got)
        except (TypeError, ValueError):
            return False
    return gold == got


async def score_one(row: dict[str, Any], result: Result) -> None:
    provider = get_provider()
    settings = get_settings()
    system = (
        prompts.EXTRACTION_SYSTEM_FEWSHOT
        if settings.extraction_strategy == "fewshot"
        else prompts.EXTRACTION_SYSTEM
    )

    try:
        extraction = await provider.extract(
            system=system,
            # The stage this message would arrive at, from the label — because
            # the extractor always has it in production, and some facts are only
            # meaningful in context: "ok" is consent at the consent step and
            # noise everywhere else. Scoring stage-blind would measure a task
            # the system never performs. No *slots* are supplied, so the model
            # still has to read every fact out of the message itself.
            user=prompts.render_extraction_prompt(row.get("stage", "qualify"), {}, row["text"]),
            schema=prompts.EXTRACTION_SCHEMA,
        )
        got = extraction.data
    except ModelError as exc:
        log.warning("extraction_failed", id=row["id"], error=str(exc))
        result.errors += 1
        got = {}

    result.total += 1
    script = row["script"]

    # --- intent -----------------------------------------------------------
    gold_intent = row["intent"]
    got_intent = got.get("intent")
    correct = got_intent == gold_intent
    result.intent_correct += correct
    result.intent_by_script[script][0] += correct
    result.intent_by_script[script][1] += 1
    if not correct:
        result.confusions[f"{gold_intent} -> {got_intent}"] += 1

    # --- slots ------------------------------------------------------------
    gold_slots = row["slots"]
    missed: list[str] = []
    for slot in SCORED_SLOTS:
        gold_has, got_has = slot in gold_slots, got.get(slot) is not None
        if gold_has and got_has and _same(slot, gold_slots[slot], got[slot]):
            result.slots[slot].tp += 1
            result.slots_by_script[script].tp += 1
        elif gold_has and got_has:
            # Present but wrong: a false positive *and* a false negative. It is
            # the worst case — the system believes something untrue.
            result.slots[slot].fp += 1
            result.slots[slot].fn += 1
            result.slots_by_script[script].fp += 1
            result.slots_by_script[script].fn += 1
            missed.append(f"{slot}: want {gold_slots[slot]!r} got {got[slot]!r}")
        elif gold_has:
            result.slots[slot].fn += 1
            result.slots_by_script[script].fn += 1
            missed.append(f"{slot}: want {gold_slots[slot]!r} got nothing")
        elif got_has:
            result.slots[slot].fp += 1
            result.slots_by_script[script].fp += 1
            missed.append(f"{slot}: invented {got[slot]!r}")

    if missed or not correct:
        result.misses.append(
            {
                "id": row["id"],
                "script": script,
                "text": row["text"],
                "intent": {"want": gold_intent, "got": got_intent, "ok": correct},
                "slots": missed,
            }
        )


class RunAborted(RuntimeError):
    """The provider stopped answering, so the run cannot produce a real number."""


# One or two failures are noise and score as misses, which is honest. A run of
# them is an outage — a daily quota, a revoked key — and scoring those as wrong
# answers reports a model failure that never happened. This exact thing consumed
# a run: 153 of 300 calls came back 429 and the "result" was a scorecard.
MAX_CONSECUTIVE_ERRORS = 5


async def run(strategy: str, limit: int | None = None) -> Result:
    settings = get_settings()
    object.__setattr__(settings, "extraction_strategy", strategy)

    result = Result(
        strategy=strategy,
        provider=settings.llm_provider,
        model=settings.gemini_extract_model if settings.llm_provider == "gemini" else "fake",
    )
    rows = load_set(limit)
    consecutive = 0
    for row in rows:
        before = result.errors
        await score_one(row, result)
        consecutive = consecutive + 1 if result.errors > before else 0
        if consecutive >= MAX_CONSECUTIVE_ERRORS:
            raise RunAborted(
                f"{consecutive} extraction calls failed in a row after "
                f"{result.total} of {len(rows)} messages — the provider is "
                "refusing, not answering badly. No score reported."
            )
    return result


def print_result(result: Result) -> None:
    print(
        f"\n  extraction · {result.provider}/{result.model} · strategy={result.strategy} "
        f"· {result.total} labelled messages\n"
    )
    print(f"  intent accuracy       {result.intent_accuracy * 100:5.1f}%")
    for script in ("latin", "devanagari", "mixed"):
        correct, total = result.intent_by_script.get(script, [0, 0])
        if total:
            print(f"    {script:16} {correct / total * 100:5.1f}%   ({correct}/{total})")

    micro = result.micro
    print(
        f"\n  slot F1 (micro)       {micro.f1 * 100:5.1f}%    "
        f"P {micro.precision * 100:.1f}%  R {micro.recall * 100:.1f}%"
    )
    print(f"\n  {'slot':18} {'P':>7} {'R':>7} {'F1':>7} {'n':>5}")
    for slot in SCORED_SLOTS:
        c = result.slots.get(slot)
        if c and c.support:
            print(
                f"  {slot:18} {c.precision * 100:6.1f}% {c.recall * 100:6.1f}% "
                f"{c.f1 * 100:6.1f}% {c.support:5d}"
            )

    print(f"\n  {'script':18} {'P':>7} {'R':>7} {'F1':>7}")
    for script in ("latin", "devanagari", "mixed"):
        c = result.slots_by_script.get(script)
        if c and (c.tp or c.fn or c.fp):
            print(
                f"  {script:18} {c.precision * 100:6.1f}% {c.recall * 100:6.1f}% {c.f1 * 100:6.1f}%"
            )

    if result.confusions:
        print("\n  most common intent confusions:")
        for pair, n in sorted(result.confusions.items(), key=lambda kv: -kv[1])[:6]:
            print(f"    {n:3d}  {pair}")
    if result.errors:
        print(f"\n  provider errors: {result.errors}")
    print()


def _write(results: list[Result]) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "strategy": r.strategy,
            "provider": r.provider,
            "model": r.model,
            "total": r.total,
            "intent_accuracy": round(r.intent_accuracy, 4),
            "intent_by_script": {
                k: {
                    "correct": v[0],
                    "total": v[1],
                    "accuracy": round(v[0] / v[1], 4) if v[1] else 0,
                }
                for k, v in r.intent_by_script.items()
            },
            "slot_micro_f1": round(r.micro.f1, 4),
            "slot_precision": round(r.micro.precision, 4),
            "slot_recall": round(r.micro.recall, 4),
            "per_slot": {
                s: {
                    "precision": round(c.precision, 4),
                    "recall": round(c.recall, 4),
                    "f1": round(c.f1, 4),
                    "support": c.support,
                }
                for s, c in r.slots.items()
                if c.support
            },
            "by_script": {
                s: {
                    "precision": round(c.precision, 4),
                    "recall": round(c.recall, 4),
                    "f1": round(c.f1, 4),
                }
                for s, c in r.slots_by_script.items()
            },
            "confusions": dict(sorted(r.confusions.items(), key=lambda kv: -kv[1])),
            "errors": r.errors,
            "misses": r.misses,
        }
        for r in results
    ]
    (ARTIFACTS / "intent_f1.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False))


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Score extraction against the labelled set.")
    parser.add_argument("--strategy", choices=("rules", "fewshot"), default="rules")
    parser.add_argument("--compare", action="store_true", help="run both strategies head to head")
    parser.add_argument("--limit", type=int, help="score only the first N messages (smoke test)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    configure_logging()
    await db.open_pool()
    await cache.open_redis()
    try:
        strategies = ("rules", "fewshot") if args.compare else (args.strategy,)
        results = [await run(s, args.limit) for s in strategies]
        _write(results)

        if args.json:
            print(json.dumps([r.strategy for r in results]))
        else:
            for result in results:
                print_result(result)
            if len(results) == 2:
                a, b = results
                print(f"  {'':22} {'rules':>10} {'fewshot':>10} {'delta':>9}")
                print(
                    f"  {'intent accuracy':22} {a.intent_accuracy * 100:9.1f}% "
                    f"{b.intent_accuracy * 100:9.1f}% "
                    f"{(b.intent_accuracy - a.intent_accuracy) * 100:+8.1f}pts"
                )
                print(
                    f"  {'slot F1':22} {a.micro.f1 * 100:9.1f}% {b.micro.f1 * 100:9.1f}% "
                    f"{(b.micro.f1 - a.micro.f1) * 100:+8.1f}pts"
                )
                print(
                    f"  {'slot precision':22} {a.micro.precision * 100:9.1f}% "
                    f"{b.micro.precision * 100:9.1f}% "
                    f"{(b.micro.precision - a.micro.precision) * 100:+8.1f}pts\n"
                )
        return 0
    finally:
        await cache.close_redis()
        await db.close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
