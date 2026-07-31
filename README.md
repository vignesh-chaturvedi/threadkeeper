# Threadkeeper

**A durable, resumable conversational sales agent for an Indian lending funnel.**
WhatsApp in, mock lender APIs out, and state that survives nine days of silence.

> The model decides what to **say** inside a stage. A deterministic graph decides
> when a stage **changes**. Consent and KYC are not things you let a ReAct loop
> improvise.

Zero real customer data, zero lender partnerships — every external API is a mock
written in this repo.

---

## Run it

```bash
cp .env.example .env
docker compose up --build
curl -s localhost:8000/health/ready | jq
```

That brings up Postgres 16 + pgvector, Redis 7, the API, and the follow-up
worker, applying migrations first. Readiness returns `200` only when both stores
answer.

Then open **http://localhost:8000/sim** — a fake WhatsApp client that posts
genuinely HMAC-signed payloads to the real webhook. Buttons for a 4-message
burst, a byte-identical redelivery, and a tampered signature, with an inspector
pane showing what ingress did with each one.

> Postgres is published on host port **5433**, not 5432, to avoid colliding with
> a locally installed Postgres. Inside the compose network it is `db:5432`.

Running the tests, or the API, outside containers:

```bash
uv sync --all-groups
uv run pytest
uv run uvicorn app.main:api --reload
```

---

## Build status

| Phase | Component | Status |
|------:|-----------|--------|
| 00 | Scaffolding, compose topology, migrations, structured logging | ✅ done |
| 01 | Idempotent webhook ingress + channel adapter | ✅ done |
| 02 | Turn coalescing: debounce + in-flight cancellation | ✅ done |
| 03 | Deterministic stage machine on a LangGraph Postgres checkpointer | ✅ done |
| 04 | Three-tier memory (working / profile / semantic) | ✅ done |
| 05 | MCP tool server over mock lender APIs | ✅ done |
| 06 | Follow-up scheduler: ZSET timers, quiet hours, 24h window | ✅ done |
| 07 | PII tokenization, consent ledger, audit log | ⬜ |
| 08 | Eval harness: simulated personas, scorecard | ⬜ |
| 09 | Hinglish / code-mixed intent + slot accuracy | ⬜ |
| 10 | Traces, funnel view, cost per conversation | ⬜ |
| 11 | Multi-stage image, Terraform → ECS Fargate | ⬜ |
| 12 | Demo video, tradeoffs write-up, failure modes | ⬜ |

---

## Architecture

```
WhatsApp BSP ──▶ FastAPI ingress ──▶ Turn buffer ──▶ Orchestrator
                 idempotent            Redis          LangGraph
                 200 fast              debounce       cancelable
                                                          │
                    ┌─────────────────────────────────────┘
                    ▼
        route → qualify → consent → kyc → offer → close   ↘ escalate
                    │
     ┌──────────────┼──────────────┬──────────────┐
     ▼              ▼              ▼              ▼
  Postgres      pgvector      MCP tools       PII vault
  state·slots   semantic      mock lender     tokenize
  ledger        recall        APIs            before LLM
     │
     ▼
  Follow-up scheduler ─── re-entry turn ───▶ Human console
  ZSET · quiet hours · 24h                   escalation packet
```

---

## Repo layout

```
threadkeeper/
├─ app/
│  ├─ ingress/          # webhook, signature check, idempotency
│  │  ├─ adapters/      #   channel seam: base protocol + whatsapp impl
│  │  ├─ webhook.py     #   HMAC → parse → dedupe → 200 → background
│  │  ├─ outbound.py    #   retry, backoff w/ jitter, dead letters
│  │  ├─ repository.py  #   ON CONFLICT DO NOTHING RETURNING id
│  │  └─ simulator.py   #   /sim — fake WhatsApp client
│  ├─ buffer/           # debounce + in-flight cancellation
│  │  ├─ coalesce.py    #   window, generation counter, settle task
│  │  └─ lock.py        #   per-conversation Redis mutex
│  ├─ graph/            # LangGraph nodes, edges, stage policy
│  │  ├─ policy.py      #   decide() — pure, no I/O, no model. The auditable core
│  │  ├─ nodes.py       #   extract + one node per stage
│  │  ├─ build.py       #   compiled graph + Postgres checkpointer
│  │  ├─ runner.py      #   one turn; persists slots + transitions
│  │  ├─ prompts.py     #   prompts, extraction schema, consent wording
│  │  ├─ escalation.py  #   the packet a human picks up
│  │  ├─ replay.py      #   time-travel replay against current code
│  │  └─ checkpointer.py#   schema setup (NOT in a migration — see the file)
│  ├─ llm/              # provider seam: gemini | fake
│  │  ├─ base.py        #   extract() and reply() are separate calls
│  │  ├─ gemini.py      #   REST via httpx, retry + token accounting
│  │  └─ fake.py        #   deterministic, offline, Hinglish-aware
│  ├─ memory/           # working / profile / semantic tiers
│  │  ├─ tokens.py      #   estimator, calibrated vs countTokens
│  │  ├─ profile.py     #   tier 2 — slots rendered as compact facts
│  │  ├─ semantic.py    #   tier 3 — pgvector over conversation summaries
│  │  └─ conflict.py    #   provenance > recency, and sticky decisions
│  ├─ tools/            # MCP server + mock lender APIs
│  │  ├─ lender.py      #   mock marketplace: rules, matrix, fault injection
│  │  ├─ registry.py    #   the six tools, defined once
│  │  ├─ guard.py       #   stage scope + preconditions. The key decision
│  │  ├─ client.py      #   guard → idempotency → invoke → audit
│  │  └─ server.py      #   MCPServer; `python -m app.tools.server`
│  ├─ scheduler/        # ZSET worker, backoff, quiet hours
│  │  ├─ policy.py      #   every timing rule, pure functions
│  │  ├─ queue.py       #   Redis ZSET + Postgres record of truth
│  │  ├─ reentry.py     #   names the drop-off point; templates outside 24h
│  │  ├─ clock.py       #   demo clock skip, shared via Redis
│  │  └─ worker.py      #   claim → check → send → reschedule
│  ├─ privacy/          # tokenizer, consent ledger, audit log
│  ├─ obs/              # traces, cost accounting, funnel metrics
│  ├─ db.py             # one psycopg3 async pool
│  ├─ cache.py          # redis client
│  ├─ logging.py        # structlog JSON + conversation_id contextvar
│  ├─ settings.py       # the only module that reads the environment
│  └─ main.py           # FastAPI factory, health probes
├─ evals/               # personas, runner, labelled intent set
│  ├─ calibrate_tokens.py  # measures the estimator against the real tokenizer
│  └─ memory_ab.py         # what tier 3 is actually worth
├─ dashboard/           # funnel + drop-off view
├─ infra/               # terraform
├─ migrations/          # alembic, hand-written DDL
├─ docker-compose.yml
└─ Dockerfile           # multi-stage, non-root
```

---

## Design decisions so far

| Decision | Why, and what was rejected |
|---|---|
| **psycopg3, not SQLAlchemy ORM** | The LangGraph Postgres checkpointer is built on psycopg3. One driver means one pool and one failure mode. The queries that matter here — `ON CONFLICT DO NOTHING`, `FOR UPDATE SKIP LOCKED`, `jsonb` — are ones the ORM would obstruct. SQLAlchemy remains installed only because Alembic requires it. |
| **Hand-written migrations, `target_metadata = None`** | There are no ORM models to autogenerate from, and pgvector indexes and skip-locked queue tables want explicit DDL. |
| **Alembic before the first table** | Retrofitting migrations onto a live schema in week 3 is miserable. The baseline migration creates zero application tables — it only enables `pgcrypto` and `vector`, proving the path works while there is nothing to lose. |
| **Split liveness and readiness** | A liveness probe that fails on a Redis blip restarts a healthy container. Liveness touches nothing; readiness touches everything and returns 503 honestly. |
| **`migrate` as a one-shot compose service** | `app` and `worker` both gate on `service_completed_successfully`, so no container ever serves traffic against an unmigrated schema, and two replicas cannot race the same migration. |
| **One config module** | Nothing outside `app/settings.py` reads `os.environ`, which is what makes `.env.example` an accurate document instead of an aspirational one. |
| **Idempotency in the schema, not the handler** | A partial unique index on `messages.provider_msg_id` plus `ON CONFLICT DO NOTHING RETURNING id`. A SELECT-then-INSERT looks correct and loses precisely the race that redelivery creates — there is a test firing 8 concurrent identical deliveries to prove the difference. |
| **Conversations keyed by an HMAC of the phone number** | A database dump is then not a directory of who was contacted. Normalisation happens first, so `+91 98765-43210` and `09876543210` resolve to one conversation rather than three. |
| **Retry policy in the sender, not the adapter** | Every channel inherits the same backoff, the same permanent-vs-transient split, and the same dead-letter table. Full jitter on the backoff, because a provider blip that fails 100 conversations otherwise produces 100 retries in the same millisecond. |
| **Debounce and invalidation are separate mechanisms** | Debounce answers "has the customer stopped typing"; the generation counter answers "is the reply I just produced still the right one". Conflating them means either a chatty user never gets a reply, or a superseded reply ships anyway. |
| **A monotonic generation counter in Redis, not just `Task.cancel()`** | `cancel()` only reaches tasks in *this* process. With two replicas the other one never hears about it, so every turn re-checks its generation immediately before sending and drops the reply if it moved. The local cancel is the fast path; the counter is the correctness guarantee. |
| **The buffer is drained only after the reply ships** | Drain-then-generate loses the customer's words whenever a turn is superseded. There is a test asserting "first thought" survives into the next turn. |
| **A locked conversation stands down rather than queues** | If another worker owns it, waiting your turn just produces the second reply this whole phase exists to prevent. |
| **The stage policy is a pure function** | `decide()` has no I/O, no model call and no framework, so every routing rule in the product is unit-tested in microseconds. Prompt-based gating would need a model call per assertion, would be non-deterministic, and would still only tell you what happened once. |
| **Extraction and reply are two calls** | One structured-output call at temperature 0 decides what is *true*; a separate call writes prose. Mixing them makes both worse: extraction can then be scored against a labelled set without a human reading anything, and the reply prompt can change without silently altering what the system believes. |
| **Consent wording is fixed, never generated** | Every other stage's words come from the model. Consent does not — a paraphrase each time would make the wording hash, and therefore the whole consent ledger, meaningless. |
| **State is duplicated into tables on purpose** | The checkpoint is the source of truth for *resuming*; `slots` and `stage_transitions` are the source of truth for *asking questions*. "How many leads reached KYC without a PAN" should be SQL, not a script that deserialises checkpoints. |
| **Every transition records which condition fired** | `reason` is not decoration. Months later "why did this conversation jump to escalate" is a row rather than a reconstruction — and it is what the Phase 10 funnel chart is built from. |
| **Checkpointer schema setup is not in a migration** | `PostgresSaver.setup()` issues `CREATE INDEX CONCURRENTLY`, which waits for every open transaction — including the migration's own. It hangs forever while holding locks. It runs as a second step after `alembic upgrade head`. |
| **Slots over retrieval** | In a funnel most questions are "what is their income", not "what did they complain about". Tier 2 is a `SELECT` rendered as four lines — exact, cheap, debuggable, identical bytes for identical facts. Tier 3 exists, scoped to one customer's prior conversations, and was measured rather than assumed. |
| **One embedding per conversation, not per message** | "hi", "ok" and "haan" are the most frequent things anyone types. A per-message index spends its top-k on greetings; a summary written once at close is the unit that answers "what happened last time". |
| **The token estimator is calibrated, not guessed** | `evals/calibrate_tokens.py` measures it against Gemini's `countTokens`. Two findings: Devanagari is *not* denser than English (4.69 vs 4.40 chars/token — the opposite of my assumption), and JSON-shaped text is (2.33), which is why the profile renders as lines. Deliberately asymmetric: it never under-estimates, because over-estimating trims history and under-estimating overflows the window. |
| **Provenance outranks recency** | A value the customer confirmed beats a newer one inferred from a passing remark. Within the same provenance, newer wins — "4 lakh, sorry, 6 lakh" is a correction. Opt-out and consent are sticky: only a *confirmed* signal can reverse them, because an extraction returning `false` usually means "not mentioned", and treating that as re-consent messages someone who said stop. |
| **One tool implementation, two doors** | MCP is how *other* agents reach these tools; in-process is how ours does. Routing every turn through IPC to reach code in the same repo adds a hop and a failure mode for no product benefit — what matters is that both paths run the same guard, idempotency and audit trail. There is a test that calls the server over JSON-RPC and gets the same refusal as the in-process caller. |
| **Two independent locks on `create_application`** | Stage scope (the guard knows graph state) *and* a precondition check in the handler (which knows the database). Prompt injection can talk a model into trying a tool; it cannot talk a Python function into returning a different answer. |
| **An offer id that was never quoted is rejected** | `create_application` reads the offer back out of the audit log rather than trusting its argument, which turns "the agent must not invent an offer" into something enforced rather than hoped for. |
| **Idempotency keys derived from intent, not randomness** | A random key per attempt makes every retry a new application — exactly the bug idempotency exists to prevent. The key is a hash of the call's meaning, and the uniqueness is a database index, so six concurrent retries still open one application. |
| **The lender fails on purpose** | ~5% of calls time out or 503. An agent whose lender never fails has a degradation path that has never executed. Rates come from a fixed matrix, never sampled — which is what makes "every number in this reply came from a tool" a checkable property. |
| **Redis is a cache, Postgres is the truth** | A ZSET answers "is anything due?" in O(log n) instead of scanning a table every two seconds. But caches get flushed, so every pending job is also a row, the claim query reads Postgres, and `reconcile()` rebuilds the ZSET. A test flushes Redis mid-flight and asserts the nudge still arrives. |
| **`FOR UPDATE SKIP LOCKED`, not a lock we wrote** | Two workers must never send the same nudge. Letting Postgres arbitrate is both correct and less code than any lock we could write — there is a test firing five concurrent claims that asserts exactly one wins. |
| **One clock for anything measuring elapsed time** | `last_in_at` is written with the scheduler's clock, not SQL `now()`. Identical in production; under a demo clock skip they diverge, and a reply the customer just sent looks hours old — so a nudge they already answered fires anyway. |
| **At most one pending nudge per conversation** | A partial unique index. Without it every inbound turn leaves its predecessor behind, and a chatty customer accumulates five nudges that all fire at once when they go quiet. |
| **Quiet hours shift, they don't drop** | A nudge scheduled for 2am IST is not one that should never happen; it is one that should happen at 9am. |
| **The simulator signs, the browser posts** | The demo exercises the real ingress path including HMAC verification, and "resend" is a byte-identical redelivery rather than a mock of one. |

---

## Measured, not assumed

| Question | Answer | How |
|---|---|---|
| Is the token estimator safe? | Never under-estimates across 13 samples; +34.7% mean over-estimate | `uv run python -m evals.calibrate_tokens` |
| Does the agent invent rates? | **No** — every number in a live `offer_match` reply matched a figure `fetch_offers` returned | manual check, automated in Phase 08 |
| What is tier 3 worth? | **0 → 100%** objection recall on a returning customer, for **+83 context tokens/turn**, 0 false positives on the control | `uv run python -m evals.memory_ab` |

The second number has a caveat worth stating: retrieval on its own bought
**nothing**. With the prior objection sitting in the prompt but only a hedged
"use if relevant" instruction, recall was 0/2 — the model correctly followed the
stage guidance instead. The gain came from telling one specific moment, the
opening turn of a return visit, to use it. The tokens were being paid either
way. Sample is 3 scenarios; Phase 08 scales it.

---

## Known gaps

Tracked honestly, phase by phase — see the build status table above for what does
not exist yet. Nothing in this repo has been run against a real WhatsApp Business
account, and by design it never will be.
