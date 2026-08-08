"""Scheduling calls across several API keys.

The rate limit this project keeps hitting is per key, not per project: the free
tier allows 15 requests/minute and 500/day against each key independently. One
key paces an eval run at 12 RPM, which is 20 minutes for the 240 calls a single
A/B arm needs and puts a 50-conversation comparison outside a day's quota
entirely. That is the reason the headline experiment was first reported at n=5
and came back inconclusive — the sample size was a quota decision, not a
statistical one.

So the provider takes a pool. Three keys is 36 RPM and 1500 calls a day, which
moves the same experiment from "does not fit" to about half an hour.

Two decisions worth stating, because both have a plausible-looking alternative:

**Earliest-free, not round-robin.** Round-robin hands the next call to the next
key in sequence even when that key is saturated and its neighbour is idle — the
caller then sleeps in front of a busy key while quota expires unused. This picks
whichever key comes free soonest, so the pool drains at the sum of its limits
rather than the worst of them.

**A 429 cools one key, it does not sleep the caller.** With a single key those
are the same action. With a pool they are not: the call belongs to a key that is
refusing, and the right move is to hand it to a different key immediately and
leave the refusing one out of the rotation for a minute. Sleeping instead spends
the pool's whole advantage waiting for the one key that already said no.

Keys never reach the logs. Every log line carries the pool index and an eight
character fingerprint, which is enough to identify a misconfigured key without
writing a credential to disk — the same reasoning as the PII vault, applied to
our own secrets.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field

from app.logging import get_logger

log = get_logger(__name__)

_DAY_SECONDS = 86_400.0


def fingerprint(key: str) -> str:
    """A stable, non-reversible handle for a key, safe to log."""
    return hashlib.sha256(key.encode()).hexdigest()[:8]


@dataclass
class _Key:
    index: int
    value: str
    fp: str
    # Monotonic timestamps of requests started inside the current window.
    sent: list[float] = field(default_factory=list)
    day: int = -1
    day_count: int = 0
    cooldown_until: float = 0.0

    def _roll_day(self, wall: float) -> None:
        today = int(wall // _DAY_SECONDS)
        if today != self.day:
            self.day, self.day_count = today, 0

    def exhausted(self, max_rpd: int, wall: float) -> bool:
        if max_rpd <= 0:
            return False
        self._roll_day(wall)
        return self.day_count >= max_rpd

    def next_slot(self, max_rpm: int, now: float) -> float:
        """The earliest monotonic time this key may send again."""
        if max_rpm > 0:
            self.sent = [t for t in self.sent if now - t < 60.0]
            if len(self.sent) >= max_rpm:
                return max(self.sent[0] + 60.0, self.cooldown_until)
        return max(now, self.cooldown_until)


@dataclass(frozen=True)
class Lease:
    """One authorised call. `index` and `fp` are the only parts safe to log."""

    key: str
    index: int
    fp: str


class KeyPool:
    """Paces calls across N keys, each with its own window and daily budget."""

    def __init__(self, keys: list[str]) -> None:
        self._keys = [_Key(index=i, value=k, fp=fingerprint(k)) for i, k in enumerate(keys)]
        self._lock = asyncio.Lock()

    def __len__(self) -> int:
        return len(self._keys)

    @property
    def fingerprints(self) -> list[str]:
        return [k.fp for k in self._keys]

    async def acquire(self, *, max_rpm: int, max_rpd: int) -> tuple[Lease, float]:
        """Reserve a slot on whichever key frees up first.

        Returns the lease and the seconds spent waiting, which the caller
        subtracts from its own latency accounting — time we chose to spend
        pacing is not time the model took.

        The sleep happens under the lock on purpose. Releasing it first would
        let every waiter compute the same free slot and wake into it together,
        which is the burst this exists to prevent.
        """
        if not self._keys:
            raise RuntimeError("no API keys configured")

        waited = 0.0
        async with self._lock:
            now, wall = time.monotonic(), time.time()

            live = [k for k in self._keys if not k.exhausted(max_rpd, wall)]
            if not live:
                # Every key has spent its day. Saying so beats 500 refusals.
                raise RuntimeError(
                    f"all {len(self._keys)} keys hit the {max_rpd}/day cap; "
                    "the quota resets at midnight UTC"
                )

            chosen = min(live, key=lambda k: (k.next_slot(max_rpm, now), k.index))
            slot = chosen.next_slot(max_rpm, now)

            if slot > now:
                waited = slot - now
                log.info(
                    "llm_rate_limited",
                    waiting_s=round(waited, 2),
                    key_index=chosen.index,
                    key_fp=chosen.fp,
                    pool_size=len(self._keys),
                    max_rpm=max_rpm,
                )
                await asyncio.sleep(waited)
                now = time.monotonic()

            chosen.sent.append(now)
            chosen._roll_day(time.time())
            chosen.day_count += 1
            return Lease(key=chosen.value, index=chosen.index, fp=chosen.fp), waited

    def penalise(self, lease: Lease, cooldown_s: float) -> None:
        """Bench a key the provider just refused, and let the pool route around it."""
        key = self._keys[lease.index]
        key.cooldown_until = time.monotonic() + cooldown_s
        log.warning(
            "llm_key_cooling_down",
            key_index=key.index,
            key_fp=key.fp,
            cooldown_s=cooldown_s,
            remaining_keys=sum(1 for k in self._keys if k.cooldown_until <= time.monotonic()),
        )

    def usage(self) -> list[dict[str, int | str]]:
        """Per-key counters, for the end-of-run summary. No key values."""
        wall = time.time()
        out: list[dict[str, int | str]] = []
        for k in self._keys:
            k._roll_day(wall)
            out.append({"index": k.index, "fp": k.fp, "today": k.day_count})
        return out
