"""The trace table, the queries built on it, and the console that renders them.

The thing worth testing hardest here is the arithmetic, not the HTML. A funnel
chart that renders beautifully and divides by the wrong denominator is worse than
no chart: it is a wrong number that looks authoritative, and someone will make a
decision on it.
"""

from __future__ import annotations

import asyncio
import itertools
import re
from decimal import Decimal

import pytest

from app import db
from app.graph import policy
from app.llm import gemini
from app.obs import cost, queries, trace

pytestmark = pytest.mark.integration


# ============================================================== PRICING
class TestCost:
    def test_a_priced_model_costs_what_the_table_says(self) -> None:
        # 1M in at $0.30 and 1M out at $2.50.
        assert cost.usd_for("gemini-3.5-flash-lite", 1_000_000, 0) == Decimal("0.30")
        assert cost.usd_for("gemini-3.5-flash-lite", 0, 1_000_000) == Decimal("2.50")

    def test_output_is_the_expensive_half(self) -> None:
        """Which is why the reply prompt is capped and extraction returns fields."""
        same = 10_000
        assert cost.usd_for("gemini-3.5-flash-lite", 0, same) > cost.usd_for(
            "gemini-3.5-flash-lite", same, 0
        )

    def test_the_fake_provider_is_free_and_says_so(self) -> None:
        assert cost.usd_for("fake", 10**6, 10**6) == 0
        assert cost.is_priced("fake")

    def test_an_unknown_model_costs_zero_but_is_not_priced(self) -> None:
        """A pricing gap must not fail a turn — but it must be answerable."""
        assert cost.usd_for("gemini-99-ultra", 10**6, 10**6) == 0
        assert not cost.is_priced("gemini-99-ultra")

    def test_it_returns_decimal_not_float(self) -> None:
        """These get summed over thousands of rows; float drift is a wrong invoice."""
        assert isinstance(cost.usd_for("gemini-3.5-flash-lite", 1, 1), Decimal)


# ============================================================== PER-TURN USAGE
class TestTurnUsage:
    def test_it_subtracts_the_running_total(self) -> None:
        """The graph counts cumulatively; a trace row must not."""
        before = {"input_tokens": 900, "output_tokens": 300, "calls": 6}
        after = {"input_tokens": 1100, "output_tokens": 340, "calls": 8}
        assert trace.turn_usage(before, after) == {
            "input_tokens": 200,
            "output_tokens": 40,
            "calls": 2,
        }

    def test_the_first_turn_has_no_prior(self) -> None:
        after = {"input_tokens": 61, "output_tokens": 24, "calls": 2}
        assert trace.turn_usage(None, after) == after

    def test_it_never_goes_negative(self) -> None:
        """A checkpoint restored from an earlier point would otherwise bill backwards."""
        got = trace.turn_usage({"input_tokens": 500}, {"input_tokens": 100})
        assert got["input_tokens"] == 0


# ============================================================== THROTTLE ACCOUNTING
class TestThrottleAccounting:
    """Latency must exclude our own rate limiter.

    A throttle is a decision about spend, not a property of the model. Left in,
    the console reported a 50-second turn at `consent` — 47 seconds of which was
    the free-tier pacing this project chose to apply.
    """

    def test_it_starts_at_zero(self) -> None:
        gemini.begin_turn()
        assert gemini.throttled_ms() == 0

    def test_it_reads_zero_outside_a_turn(self) -> None:
        """A provider call from a script has no account open; that is not an error."""
        import contextvars

        assert contextvars.Context().run(gemini.throttled_ms) == 0

    async def test_a_wait_inside_a_child_task_is_visible_to_the_caller(self) -> None:
        """The bug this shape exists to prevent.

        The graph runs nodes in child tasks, and a task gets a *copy* of the
        context — so `ContextVar.set()` inside a node never reaches the runner.
        Mutating one shared dict does. Asserted directly rather than trusted,
        because the first version silently recorded every wait and threw it away.
        """
        gemini.begin_turn()

        async def node() -> None:
            gemini._throttle.get()["ms"] += 1500

        await asyncio.create_task(node())
        assert gemini.throttled_ms() == 1500

    async def test_turns_do_not_bill_each_other(self) -> None:
        """Two conversations in flight must keep separate accounts."""

        async def turn(wait_ms: int) -> int:
            gemini.begin_turn()
            gemini._throttle.get()["ms"] += wait_ms
            await asyncio.sleep(0)
            return gemini.throttled_ms()

        first, second = await asyncio.gather(turn(1000), turn(4000))
        assert (first, second) == (1000, 4000)


# ============================================================== THE FUNNEL
class TestFunnel:
    async def test_it_reports_every_stage_even_with_no_traffic(self, live_db: None) -> None:
        """A stage missing from the chart reads as a stage nobody drops at."""
        rows = await queries.funnel()
        assert [r["stage"] for r in rows] == queries.FUNNEL_STAGES

    async def test_intent_route_is_the_denominator_not_a_step(self) -> None:
        """Every conversation starts there, so a bar at 100% says nothing."""
        assert "intent_route" in policy.STAGES
        assert "intent_route" not in queries.FUNNEL_STAGES

    async def test_the_first_stage_has_no_step_conversion(self, live_db: None) -> None:
        rows = await queries.funnel()
        assert rows[0]["pct_of_previous"] is None
        assert rows[0]["dropped_here"] is None

    async def test_later_stages_report_conversion_from_the_one_before(self, live_db: None) -> None:
        rows = await queries.funnel()
        for previous, row in itertools.pairwise(rows):
            if previous["reached"]:
                expected = round(row["reached"] / previous["reached"] * 100, 1)
                assert row["pct_of_previous"] == pytest.approx(expected)
                assert row["dropped_here"] == max(0, previous["reached"] - row["reached"])

    async def test_the_gated_funnel_never_widens(self, live_db: None) -> None:
        """From `consent` on, each stage requires the one before it.

        `qualify` is deliberately excluded, and finding out why is what this test
        was originally for. It asserted monotonicity across all five stages,
        which looks obviously true and is not: `qualify` is not a step, it is
        where the policy sends a conversation whose product is unknown. A
        customer whose first message is "personal loan chahiye" is routed
        straight to `consent` and never appears in `qualify` at all — so the
        chart can legitimately show more leads reaching consent than qualify.

        The invariant that does hold is over the gated stages, because
        `decide()` will not reach any of them without the previous one's
        precondition. If *this* widens, the transition log contains a path that
        skipped a gate, which in a consent-bearing funnel is an incident.
        """
        rows = {r["stage"]: r["reached"] for r in await queries.funnel()}
        gated = [rows[s] for s in ("consent", "kyc_collect", "offer_match", "close")]
        assert gated == sorted(gated, reverse=True), gated

    async def test_qualify_is_a_remediation_stage_not_a_step(self) -> None:
        """The reason the invariant above is scoped. Asserted against the policy
        rather than against data, so it stays true when the seed changes."""
        straight_to_consent = policy.decide({"slots": {"product": "personal_loan"}, "consent": {}})
        assert straight_to_consent.stage == "consent"

        no_product = policy.decide({"slots": {}, "consent": {}})
        assert no_product.stage == "qualify"
        assert no_product.reason == "product_unknown"


# ============================================================== UNIT ECONOMICS
class TestUnitEconomics:
    async def test_money_is_scoped_to_priced_traffic(self, live_db: None) -> None:
        """Averaging real spend over free `fake` turns reports a cheaper system."""
        economics = await queries.unit_economics()
        assert economics["priced_conversations"] <= economics["conversations"]
        assert economics["priced_turns"] <= economics["turns"]

    async def test_per_conversation_divides_by_priced_conversations(self, live_db: None) -> None:
        e = await queries.unit_economics()
        if e["priced_conversations"]:
            assert e["usd_per_conversation"] == e["total_usd"] / e["priced_conversations"]

    async def test_no_sales_reads_as_unknown_not_free(self, live_db: None) -> None:
        """None and $0.00 are different claims. Only one of them is ever true."""
        e = await queries.unit_economics()
        assert e["usd_per_sale"] is None or e["usd_per_sale"] > 0

    async def test_an_empty_database_does_not_divide_by_zero(
        self, monkeypatch: pytest.MonkeyPatch, live_db: None
    ) -> None:
        async def empty(*_: object, **__: object) -> dict[str, object]:
            return dict.fromkeys(
                [
                    "conversations",
                    "turns",
                    "priced_conversations",
                    "priced_turns",
                    "total_usd",
                    "total_tokens",
                    "sales",
                    "priced_sales",
                    "escalated",
                    "degraded_turns",
                ],
                0,
            )

        monkeypatch.setattr(db, "fetch_one", empty)
        e = await queries.unit_economics()
        assert e["usd_per_conversation"] == 0
        assert e["usd_per_sale"] is None
        assert e["conversion_pct"] == 0.0


# ============================================================== THE CONSOLE
class TestConsole:
    async def test_the_dashboard_renders(self, live_app) -> None:  # type: ignore[no-untyped-def]
        resp = await live_app.get("/console")
        assert resp.status_code == 200
        assert "Funnel" in resp.text

    async def test_the_chart_and_the_table_come_from_one_query(self, live_app) -> None:  # type: ignore[no-untyped-def]
        """The whole reason the chart is generated server-side rather than fetched.

        Asserted against the figures the chart renders, parsed back out of it,
        rather than against a formatted string. The first version looked for the
        literal "27 · 54%" and broke the moment the count and the percentage
        became separate elements — it was pinning the markup, not the invariant,
        and the invariant is that these numbers cannot disagree with the API.
        """
        page = (await live_app.get("/console")).text
        rows = (await live_app.get("/console/api/funnel")).json()["funnel"]

        rendered = re.findall(
            r'<div class="fl">([a-z_]+)</div>.*?<b>(\d+)</b><span>(\d+)%</span>',
            page,
            flags=re.S,
        )
        assert rendered, "the funnel chart rendered no rows"
        assert [stage for stage, _, _ in rendered] == [r["stage"] for r in rows]
        for (_, reached, pct), row in zip(rendered, rows, strict=True):
            assert int(reached) == row["reached"]
            assert int(pct) == round(row["pct_of_total"])

    async def test_the_json_endpoints_answer(self, live_app) -> None:  # type: ignore[no-untyped-def]
        cost_payload = (await live_app.get("/console/api/cost")).json()
        assert "usd_per_conversation" in cost_payload
        assert isinstance(cost_payload["by_stage"], list)

    async def test_a_malformed_id_is_404_not_500(self, live_app) -> None:  # type: ignore[no-untyped-def]
        """Postgres raises on a bad uuid; that is a bad request, not an outage."""
        assert (await live_app.get("/console/c/not-a-uuid")).status_code == 404

    async def test_an_unknown_conversation_is_404(self, live_app) -> None:  # type: ignore[no-untyped-def]
        missing = "00000000-0000-0000-0000-000000000000"
        assert (await live_app.get(f"/console/c/{missing}")).status_code == 404

    async def test_the_inspector_renders_a_real_conversation(self, live_app) -> None:  # type: ignore[no-untyped-def]
        row = await db.fetch_one("SELECT conversation_id FROM turns LIMIT 1")
        if row is None:
            pytest.skip("no traced turns — run evals.seed_console")
        resp = await live_app.get(f"/console/c/{row['conversation_id']}")
        assert resp.status_code == 200
        assert "Replay" in resp.text

    async def test_customer_text_is_escaped(self, live_app, clean_conversation: str) -> None:  # type: ignore[no-untyped-def]
        """A transcript is untrusted input rendered into a page.

        Both directions asserted deliberately. The first version of this test
        recorded no inbound message, so the payload never reached the page and
        "no <script> in the output" passed for the wrong reason — a negative
        assertion with nothing behind it.
        """
        from app.graph.runner import run_turn
        from app.ingress import repository
        from app.ingress.events import InboundEvent
        from app.privacy.refs import customer_ref

        payload = "<script>alert('xss')</script> loan chahiye"
        ref = customer_ref(clean_conversation)
        conversation = await repository.get_or_create_conversation("whatsapp", ref)
        cid = str(conversation["id"])

        await repository.record_inbound(
            InboundEvent(
                channel="whatsapp",
                provider_msg_id=f"xss-{cid}",
                customer_ref=ref,
                text=payload,
            ),
            cid,
        )
        await run_turn(cid, payload)

        page = (await live_app.get(f"/console/c/{cid}")).text
        assert "&lt;script&gt;alert" in page, "the payload must actually reach the page"
        assert "<script>alert" not in page


# ============================================================== THE CLOSE PATH
class TestTheFunnelCanFinish:
    """Until Phase 10 the only routes to `close` were opt-out and consent refusal.

    Every close was a failure, `applications` stayed empty, and cost-per-sale had
    no numerator. These pin the path that fixes it.
    """

    def test_accepting_a_shown_offer_closes(self) -> None:
        decision = policy.decide(
            {
                "slots": {
                    "product": "personal_loan",
                    "income_band": "50k_1l",
                    "pan_status": "available",
                    "offer_accepted": True,
                },
                "consent": {"granted": True},
                "last_offer": {"offer_id": "of_1"},
            }
        )
        assert decision.stage == "close"
        assert decision.reason == "offer_accepted"

    def test_accepting_before_any_offer_was_shown_does_not(self) -> None:
        """ "haan" with no figures quoted is agreement to nothing."""
        decision = policy.decide(
            {
                "slots": {
                    "product": "personal_loan",
                    "income_band": "50k_1l",
                    "pan_status": "available",
                    "offer_accepted": True,
                },
                "consent": {"granted": True},
                "last_offer": None,
            }
        )
        assert decision.stage == "offer_match"

    def test_an_opt_out_still_beats_an_acceptance(self) -> None:
        decision = policy.decide(
            {
                "slots": {
                    "product": "personal_loan",
                    "income_band": "50k_1l",
                    "pan_status": "available",
                    "offer_accepted": True,
                    "opted_out": True,
                },
                "consent": {"granted": True},
                "last_offer": {"offer_id": "of_1"},
            }
        )
        assert decision.reason == "customer_opted_out"
