"""Push traffic through the real pipeline so the console has something to show.

Not fixtures. Every conversation here runs the actual graph, the actual policy,
the actual tools and the actual trace writer — the only substitution is the model
provider, which is `fake` so this is offline, deterministic and free. A dashboard
demonstrated against hand-written rows is a screenshot of a template.

    uv run python -m evals.seed_console --conversations 24

Deterministic by default: the same seed produces the same funnel, so the
README's chart does not change shape every time it is regenerated.
"""

from __future__ import annotations

import argparse
import asyncio
import random
from typing import Any

from app import cache, db
from app.graph.runner import run_turn
from app.ingress import repository
from app.ingress.events import InboundEvent
from app.logging import configure_logging, get_logger
from app.privacy.refs import customer_ref
from app.scheduler import queue
from app.settings import get_settings

log = get_logger(__name__)

# A live seed exists to price the funnel, not to populate it. Eight conversations
# is ~35 turns and ~70 model calls — enough for a couple of closed sales and a
# real cost-per-sale figure, and about a seventh of a free tier's daily budget.
LIVE_CAP = 8
# Offset so live and fake seeds never collide on a customer_ref.
LIVE_OFFSET = 500

# Scripts chosen to produce a funnel with a real shape rather than a flattering
# one: most leads stall before offers, which is what a lending funnel does.
# Weights are how often each script runs, not a claim about real traffic.
SCRIPTS: list[tuple[str, int, list[str]]] = [
    (
        "completes",
        4,
        [
            "hi, personal loan chahiye",
            "5 lakh chahiye, salary 80k hai",
            "haan theek hai, share kar dijiye",
            "PAN hai mere paas",
            "offer dikhaiye",
            "haan pehla wala theek hai, apply kar dijiye",
        ],
    ),
    (
        "stalls_at_kyc",
        6,
        [
            "loan ke baare mein jaanna hai",
            "personal loan, 3 lakh, income 45k",
            "haan sahi hai",
            "PAN nahi hai abhi",
        ],
    ),
    (
        "objects_then_leaves",
        5,
        [
            "मुझे पर्सनल लोन चाहिए",
            "salary 60k hai, 4 lakh chahiye",
            "ब्याज दर कितनी है",
            "bahut zyada hai bhai",
        ],
    ),
    (
        "opts_out",
        3,
        ["hello", "loan chahiye tha", "nahi rehne do, band karo ye messages"],
    ),
    (
        "escalates",
        2,
        ["business loan chahiye 10 lakh ka", "kisi insaan se baat karao"],
    ),
    (
        "ghosts_early",
        4,
        ["hi"],
    ),
]


async def _seed_one(name: str, turns: list[str], index: int) -> dict[str, Any]:
    # A distinct phone per conversation, so each gets its own customer_ref and
    # its own thread rather than resuming one long shared conversation. The ref
    # is derived exactly as the webhook derives it — a seed that bypassed the
    # HMAC would produce conversations the rest of the system cannot find.
    #
    # Live seeds use a separate number range. Sharing it would make the second
    # run *resume* the first run's conversations, leaving single threads with
    # some turns priced at zero and some at the real rate — and a cost-per-
    # conversation figure averaged over two different providers.
    ref = customer_ref(f"+9199{index:08d}")
    conversation = await repository.get_or_create_conversation("whatsapp", ref)
    conversation_id = str(conversation["id"])

    for i, text in enumerate(turns):
        await repository.record_inbound(
            InboundEvent(
                channel="whatsapp",
                provider_msg_id=f"seed-{index}-{i}",
                customer_ref=ref,
                text=text,
            ),
            conversation_id,
        )
        reply = await run_turn(conversation_id, text)
        await repository.record_outbound(conversation_id, reply, None)

    return {"script": name, "conversation_id": conversation_id, "turns": len(turns)}


async def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the console with real pipeline traffic.")
    parser.add_argument("--conversations", type=int, default=24)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete this seeder's previous conversations first (its own number range only)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=f"allow a live provider, capped at {LIVE_CAP} conversations",
    )
    args = parser.parse_args()

    configure_logging()
    settings = get_settings()
    if settings.llm_provider != "fake" and not args.live:
        # A 24-conversation seed is ~120 turns and ~240 model calls. Doing that
        # against a paid provider by accident is a bad surprise, and against a
        # free tier it is a wasted day of quota.
        print("refusing to seed against a live provider — set TK_LLM_PROVIDER=fake, or pass --live")
        return 2

    if args.live:
        # The cap is the point of the flag. Cost per conversation has to be
        # measured against a real price list to mean anything — the fake
        # provider is priced at zero and would report a free funnel — but that
        # is worth a few dozen calls, not a few hundred.
        if settings.llm_provider == "fake":
            print("--live needs a live provider — set TK_LLM_PROVIDER=gemini")
            return 2
        args.conversations = min(args.conversations, LIVE_CAP)
        print(f"  live provider: {settings.gemini_reply_model}, {args.conversations} conversations")

    offset = LIVE_OFFSET if args.live else 0
    rng = random.Random(args.seed)  # noqa: S311 — seeding a demo, not a key
    pool = [(name, turns) for name, weight, turns in SCRIPTS for _ in range(weight)]

    await db.open_pool()
    await cache.open_redis()
    try:
        if args.reset:
            # Scoped to this seeder's own number range, and nothing else. The
            # seeder resumes conversations it has already created — same phone,
            # same customer_ref — so a second run appends turns rather than
            # replacing them, and the funnel slowly stops describing any cohort.
            span = max(args.conversations, LIVE_CAP)
            refs = [customer_ref(f"+9199{i + offset:08d}") for i in range(span)]
            deleted = await db.fetch_all(
                "DELETE FROM conversations WHERE customer_ref = ANY(%s) RETURNING id", refs
            )
            print(f"  reset: removed {len(deleted)} previously seeded conversations")

        results = []
        for i in range(args.conversations):
            name, turns = pool[i % len(pool)]
            # Truncate some conversations early: real customers stop mid-funnel,
            # and a seed where everyone finishes their script produces a funnel
            # with no drop-off to look at.
            cut = len(turns) if rng.random() > 0.25 else rng.randint(1, len(turns))
            results.append(await _seed_one(name, turns[:cut], i + offset))

        # Every live conversation armed a follow-up on its last turn, which is
        # correct for a customer and wrong for demo data: twenty-four nudges
        # would fire on the next worker pass, and in the meantime they fill the
        # claim batch that the scheduler's own tests rely on.
        cancelled = 0
        for row in results:
            cancelled += await queue.cancel(row["conversation_id"], "seed_data")

        print(f"\n  seeded {len(results)} conversations, cancelled {cancelled} nudges")
        for row in results:
            print(f"    {row['script']:<22} {row['turns']} turns  {row['conversation_id'][:8]}")
        print("\n  open http://localhost:8000/console\n")
    finally:
        await cache.close_redis()
        await db.close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
