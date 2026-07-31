"""Every timing rule in the product, asserted without a database or a clock.

The same argument the stage policy makes: the parts that decide things should be
testable in microseconds. "Never message anyone at 3am" is a rule you want to be
certain about, and certainty here costs nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.scheduler import policy
from app.scheduler.policy import IST


def ist(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=IST)


# ============================================================== QUIET HOURS
class TestQuietHours:
    @pytest.mark.parametrize(
        ("hour", "quiet"),
        [
            (0, True),
            (3, True),
            (8, True),
            (9, False),
            (12, False),
            (17, False),
            (20, False),
            (21, True),
            (23, True),
        ],
    )
    def test_the_window_is_21_to_09_ist(self, hour: int, quiet: bool) -> None:
        assert policy.is_quiet(ist(2026, 7, 15, hour)) is quiet

    def test_a_3am_job_moves_to_9am_the_same_day(self) -> None:
        moved = policy.next_allowed_slot(ist(2026, 7, 15, 3, 30))
        assert moved.astimezone(IST) == ist(2026, 7, 15, 9, 0)

    def test_a_10pm_job_moves_to_9am_the_next_day(self) -> None:
        """The bug worth having a test for: not 9am *today*, which is in the past."""
        moved = policy.next_allowed_slot(ist(2026, 7, 15, 22, 10))
        assert moved.astimezone(IST) == ist(2026, 7, 16, 9, 0)

    def test_a_daytime_job_is_left_alone(self) -> None:
        at = ist(2026, 7, 15, 14, 30)
        assert policy.next_allowed_slot(at) == at

    def test_the_shifted_slot_is_never_quiet(self) -> None:
        """Property: whatever goes in, what comes out is sendable."""
        for hour in range(24):
            for minute in (0, 31, 59):
                moved = policy.next_allowed_slot(ist(2026, 7, 15, hour, minute))
                assert not policy.is_quiet(moved), f"{hour}:{minute} → {moved}"

    def test_the_shift_never_goes_backwards(self) -> None:
        for hour in range(24):
            at = ist(2026, 7, 15, hour, 17)
            assert policy.next_allowed_slot(at) >= at

    def test_quiet_hours_are_evaluated_in_ist_not_utc(self) -> None:
        """The whole point of the timezone. 20:00 UTC is 01:30 IST — quiet."""
        at_utc = datetime(2026, 7, 15, 20, 0, tzinfo=UTC)
        assert at_utc.astimezone(IST).hour == 1
        assert policy.is_quiet(at_utc) is True


# ================================================================= BACKOFF
class TestBackoff:
    def test_the_schedule_is_the_documented_one(self) -> None:
        """2h → 1d → 3d → 7d. Written down so it can be pointed at."""
        assert policy.BACKOFF == (
            timedelta(hours=2),
            timedelta(days=1),
            timedelta(days=3),
            timedelta(days=7),
        )
        assert policy.MAX_ATTEMPTS == 4

    def test_delays_widen(self) -> None:
        """Someone who ignored three messages is not persuaded by a faster fourth."""
        delays = [policy.delay_for(i) for i in range(policy.MAX_ATTEMPTS)]
        assert delays == sorted(delays)

    def test_it_stops_after_four(self) -> None:
        assert not policy.is_exhausted(3)
        assert policy.is_exhausted(4)
        assert policy.is_exhausted(99)

    def test_an_over_run_attempt_does_not_crash(self) -> None:
        """Defensive: index out of range here would wedge a conversation."""
        assert policy.delay_for(50) == policy.BACKOFF[-1]

    def test_scheduling_respects_quiet_hours(self) -> None:
        """8pm + 2h is 10pm, which nobody should receive."""
        due = policy.schedule_at(ist(2026, 7, 15, 20, 0), attempt=0)
        assert due.astimezone(IST) == ist(2026, 7, 16, 9, 0)


# ========================================================== SERVICE WINDOW
class TestServiceWindow:
    def test_inside_24h_is_free_form(self) -> None:
        now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
        assert policy.within_service_window(now, now - timedelta(hours=23)) is True

    def test_outside_24h_needs_a_template(self) -> None:
        now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
        assert policy.within_service_window(now, now - timedelta(hours=25)) is False

    def test_exactly_24h_is_outside(self) -> None:
        """Boundaries are where policy violations live."""
        now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
        assert policy.within_service_window(now, now - timedelta(hours=24)) is False

    def test_never_having_replied_means_template_only(self) -> None:
        """No inbound message means no window was ever opened."""
        now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
        assert policy.within_service_window(now, None) is False


# ========================================================== STOP CONDITIONS
class TestStopConditions:
    def _due(self) -> datetime:
        return datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

    @pytest.mark.parametrize("status", ["won", "lost", "opted_out", "escalated"])
    def test_terminal_statuses_cancel(self, status: str) -> None:
        assert policy.cancellation_reason(status, None, self._due()) == f"conversation_{status}"

    def test_a_reply_after_scheduling_cancels(self) -> None:
        """The done-when's second half: a lead who replied gets no nudge."""
        due = self._due()
        reason = policy.cancellation_reason("active", due + timedelta(minutes=5), due)
        assert reason == "customer_replied"

    def test_a_reply_before_scheduling_does_not_cancel(self) -> None:
        """That reply is *why* the nudge exists. Cancelling on it sends nothing, ever."""
        due = self._due()
        assert policy.cancellation_reason("active", due - timedelta(hours=2), due) is None

    def test_an_active_silent_conversation_proceeds(self) -> None:
        assert policy.cancellation_reason("active", None, self._due()) is None
