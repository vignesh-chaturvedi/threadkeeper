"""When to nudge, and when not to.

One file, deliberately, because the plan's advice is to be able to point at it:
every timing rule in the product is here, none of it is in a prompt, and all of
it is a pure function of a timestamp and a conversation's state.

Three rules, in descending order of how much trouble getting them wrong causes:

  1. **Stop conditions beat everything.** Opted out, escalated, won, or simply
     replied — each cancels the pending nudge. A follow-up sent to someone who
     already answered reads as a system that is not listening.
  2. **Quiet hours.** Never between 21:00 and 09:00 IST. A loan marketing
     message at 3am is how a brand ends up reported, and shifting the job to the
     next allowed slot is trivially cheaper than dropping it.
  3. **Backoff.** 2h → 1d → 3d → 7d, four attempts, then stop. The gaps widen
     because someone who ignored three messages is not persuaded by a fourth
     arriving sooner.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Delay before each attempt. Index is the attempt about to be made.
BACKOFF: tuple[timedelta, ...] = (
    timedelta(hours=2),
    timedelta(days=1),
    timedelta(days=3),
    timedelta(days=7),
)
MAX_ATTEMPTS: int = len(BACKOFF)

QUIET_START_HOUR = 21  # 21:00 IST — stop sending
QUIET_END_HOUR = 9  # 09:00 IST — start again

# WhatsApp's customer-service window. Inside it you may send free-form text;
# outside it, only an approved template.
SERVICE_WINDOW = timedelta(hours=24)

# Conversation statuses that mean "there is nothing to follow up".
TERMINAL_STATUSES = frozenset({"won", "lost", "opted_out", "escalated"})


def is_quiet(at: datetime) -> bool:
    """True if this instant falls in IST quiet hours."""
    hour = at.astimezone(IST).hour
    return hour >= QUIET_START_HOUR or hour < QUIET_END_HOUR


def next_allowed_slot(at: datetime) -> datetime:
    """The first moment at or after `at` that is not quiet hours.

    Shifts rather than drops. A nudge scheduled for 2am is not a nudge that
    should never happen; it is one that should happen at 9am.
    """
    local = at.astimezone(IST)

    if not is_quiet(local):
        return at

    if local.hour >= QUIET_START_HOUR:
        local = local + timedelta(days=1)

    opens = local.replace(hour=QUIET_END_HOUR, minute=0, second=0, microsecond=0)
    return opens.astimezone(at.tzinfo)


def delay_for(attempt: int) -> timedelta:
    """How long to wait before attempt number `attempt` (0-indexed)."""
    return BACKOFF[min(attempt, len(BACKOFF) - 1)]


def schedule_at(now: datetime, attempt: int) -> datetime:
    """Due time for the next attempt, already moved out of quiet hours."""
    return next_allowed_slot(now + delay_for(attempt))


def is_exhausted(attempt: int) -> bool:
    return attempt >= MAX_ATTEMPTS


def within_service_window(now: datetime, last_in_at: datetime | None) -> bool:
    """Free-form allowed, or template required?

    No inbound message at all means no window was ever opened — template only.
    """
    if last_in_at is None:
        return False
    return (now - last_in_at) < SERVICE_WINDOW


def cancellation_reason(status: str, last_in_at: datetime | None, due_at: datetime) -> str | None:
    """Why this job should be dropped instead of sent, or None to proceed."""
    if status in TERMINAL_STATUSES:
        return f"conversation_{status}"
    if last_in_at is not None and last_in_at > due_at:
        # They replied after the job was scheduled. The nudge is answered.
        return "customer_replied"
    return None
