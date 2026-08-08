"""The key pool: scheduling, failover, and the daily budget.

No network here. The pool's whole job is deciding *which* key and *when*, which
is a pure scheduling question — so these run in milliseconds against a fake
clock rather than by actually waiting a minute for a window to roll.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.llm.keypool import KeyPool, fingerprint
from app.settings import Settings

KEYS = ["key-alpha", "key-bravo", "key-charlie"]


# --------------------------------------------------------------- configuration
def test_pool_dedupes_repeated_keys() -> None:
    """The same key twice is one budget, not two.

    A copy-pasted `.env` is the realistic way this happens, and the symptom
    would be the pool cheerfully spending double the limit it thinks it has.
    """
    settings = Settings(
        gemini_api_key="key-alpha",
        gemini_api_keys="key-bravo, key-alpha ,key-bravo",
    )
    assert settings.gemini_key_pool == ["key-alpha", "key-bravo"]


def test_pool_ignores_blank_entries() -> None:
    """Trailing commas and stray whitespace are how humans write env vars."""
    settings = Settings(gemini_api_key="", gemini_api_keys="a, ,b,")
    assert settings.gemini_key_pool == ["a", "b"]


def test_fingerprint_is_stable_and_not_the_key() -> None:
    fp = fingerprint("key-alpha")
    assert fp == fingerprint("key-alpha")
    assert fp != fingerprint("key-bravo")
    assert len(fp) == 8
    assert "key-alpha" not in fp


# ------------------------------------------------------------------ scheduling
async def test_spreads_across_keys_before_waiting() -> None:
    """Three keys at 1 RPM serve three immediate calls, one each.

    This is the property round-robin also has. The next test is the one that
    separates them.
    """
    pool = KeyPool(KEYS)
    seen = []
    for _ in range(3):
        lease, waited = await pool.acquire(max_rpm=1, max_rpd=0)
        assert waited == 0.0
        seen.append(lease.index)
    assert sorted(seen) == [0, 1, 2]


async def test_routes_around_a_benched_key_instead_of_waiting() -> None:
    """A 429'd key is skipped; the caller does not pay for its cooldown.

    Round-robin fails here: it would hand the next call to the cooling key and
    sleep in front of it while two idle keys sat unused.
    """
    pool = KeyPool(KEYS)
    lease, _ = await pool.acquire(max_rpm=0, max_rpd=0)
    pool.penalise(lease, cooldown_s=60.0)

    for _ in range(4):
        nxt, waited = await pool.acquire(max_rpm=0, max_rpd=0)
        assert nxt.index != lease.index, "a cooling key must stay out of the rotation"
        assert waited == 0.0


async def test_saturated_pool_waits_only_when_every_key_is_busy() -> None:
    """Three keys at 1 RPM: the fourth call is the first one that waits."""
    pool = KeyPool(KEYS)
    for _ in range(3):
        _, waited = await pool.acquire(max_rpm=1, max_rpd=0)
        assert waited == 0.0

    # Every key now reports a slot roughly a minute out. Assert that directly
    # rather than sleeping for one.
    now = time.monotonic()
    for key in pool._keys:
        assert 55.0 < key.next_slot(max_rpm=1, now=now) - now <= 60.0

    task = asyncio.create_task(pool.acquire(max_rpm=1, max_rpd=0))
    await asyncio.sleep(0.05)
    assert not task.done(), "the fourth call must be pacing, not proceeding"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_single_key_pool_still_paces() -> None:
    """The degenerate case has to keep working — one key is the common setup."""
    pool = KeyPool(["only-one"])
    lease, waited = await pool.acquire(max_rpm=5, max_rpd=0)
    assert (lease.index, waited) == (0, 0.0)
    assert len(pool) == 1


# --------------------------------------------------------------- daily budget
async def test_exhausted_key_leaves_the_pool() -> None:
    """A key that spent its day stops being offered work.

    Without this the pool keeps handing calls to a key that can only 429, and
    every one of them burns two retries on the way to failing.
    """
    pool = KeyPool(["a", "b"])
    for _ in range(2):
        await pool.acquire(max_rpm=0, max_rpd=2)  # spend key a
        await pool.acquire(max_rpm=0, max_rpd=2)  # spend key b

    with pytest.raises(RuntimeError, match=r"2/day cap"):
        await pool.acquire(max_rpm=0, max_rpd=2)


async def test_daily_cap_of_zero_means_unlimited() -> None:
    pool = KeyPool(["a"])
    for _ in range(50):
        await pool.acquire(max_rpm=0, max_rpd=0)
    assert pool.usage()[0]["today"] == 50


async def test_usage_reports_counts_without_keys() -> None:
    """The end-of-run summary must be safe to paste into a report."""
    pool = KeyPool(KEYS)
    await pool.acquire(max_rpm=0, max_rpd=0)
    rows = pool.usage()

    assert sum(int(r["today"]) for r in rows) == 1
    rendered = str(rows)
    for key in KEYS:
        assert key not in rendered, "usage() must never carry a key value"


async def test_empty_pool_is_an_error_not_a_hang() -> None:
    with pytest.raises(RuntimeError, match="no API keys"):
        await KeyPool([]).acquire(max_rpm=0, max_rpd=0)
