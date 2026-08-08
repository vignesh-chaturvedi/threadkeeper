"""Running the scheduler inside the API process, and the sandbox opt-in.

Both exist for the same reason: a free tier gives you one process and one public
URL. Neither is how this should be deployed — the compose topology and the
Terraform both run api and worker as separate services — so the danger is not
that the flags fail, it is that they turn on when nobody asked.

These are settings-level tests on purpose. The poll loop itself is covered by
`test_scheduler.py` against the real stores; what is unproven here is the wiring.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from app.main import lifespan
from app.scheduler import worker
from app.settings import Settings

# The vault key is the one test_ingress_unit.py already uses, reused rather than
# reinvented: .gitleaks.toml sanctions it by name, and its own rule for that list
# is that a value belongs there only if using it in production is impossible.
# A second high-entropy placeholder would mean a second allowlist entry, and an
# allowlist that grows every time someone writes a test stops being a decision.
PROD = {
    "env": "prod",
    "whatsapp_app_secret": "real-secret",
    "customer_ref_secret": "real-ref-secret",
    "vault_key": "cHJvZC12YXVsdC1rZXktMzItYnl0ZXMtZXhhY3RseSE=",
}


# ------------------------------------------------------------------ defaults
def test_both_flags_are_off_by_default() -> None:
    """Nothing about the normal deployment changes because these exist."""
    s = Settings()
    assert s.run_worker_in_process is False
    assert s.demo_sandbox is False
    assert s.scheduler_poll_interval_s == 2.0


def test_poll_interval_must_be_positive() -> None:
    """Zero would spin the loop as fast as Redis can answer."""
    with pytest.raises(ValueError):
        Settings(scheduler_poll_interval_s=0)


# ----------------------------------------------------------- simulator rule
def test_simulator_mounts_locally() -> None:
    assert Settings(env="local").mount_simulator is True


def test_prod_refuses_the_simulator_by_default() -> None:
    """The existing guarantee. A deployed /sim is a forged-traffic endpoint."""
    s = Settings(**PROD)
    assert s.enable_simulator is False
    assert s.mount_simulator is False


def test_sandbox_opt_in_mounts_it_in_prod() -> None:
    """...and only when someone set the flag whose name says what it does."""
    s = Settings(**PROD, demo_sandbox=True)
    assert s.mount_simulator is True


def test_sandbox_does_not_bypass_the_secret_checks() -> None:
    """The opt-in relaxes exactly one thing, and it is not the credentials.

    A sandbox running on the shipped dev vault key would be a public endpoint
    tokenizing PII under a key that is in the repository.
    """
    with pytest.raises(ValueError, match="TK_VAULT_KEY"):
        Settings(
            env="prod",
            whatsapp_app_secret="real-secret",
            customer_ref_secret="real-ref-secret",
            demo_sandbox=True,
        )


def test_enable_simulator_false_still_wins() -> None:
    """Two switches, and the off one is not overridden by the sandbox flag."""
    s = Settings(env="local", enable_simulator=False, demo_sandbox=True)
    assert s.mount_simulator is False


# --------------------------------------------------------------- the wiring
def test_poll_loop_takes_no_stores_of_its_own() -> None:
    """The in-process caller must not open or close pools the lifespan owns.

    `run()` is the standalone entrypoint and does own them; `poll_loop` is the
    half the API process hosts. Conflating them would close the pool out from
    under every in-flight turn on shutdown.
    """
    src = inspect.getsource(worker.poll_loop)
    for forbidden in ("open_pool", "close_pool", "open_redis", "close_redis"):
        assert forbidden not in src, f"poll_loop must not call {forbidden}"

    run_src = inspect.getsource(worker.run)
    assert "open_pool" in run_src and "close_pool" in run_src


@pytest.mark.integration
async def test_poll_loop_stops_when_asked(live_db: None) -> None:
    """A set event ends the loop rather than waiting out the interval.

    Integration because the loop reconciles the ZSET from Postgres on entry —
    rebuilding pending nudges after a cache flush is startup work, not polling.
    """
    stopping = asyncio.Event()
    stopping.set()
    await asyncio.wait_for(worker.poll_loop(stopping), timeout=10.0)


def test_lifespan_cancels_the_worker_before_draining_turns() -> None:
    """Ordering, not existence.

    Draining first would let the worker claim a follow-up during the drain and
    then lose it when the stores close — a nudge marked 'running' that never
    ran, and no log line saying why.
    """
    src = inspect.getsource(lifespan)
    cancel_at = src.index("worker_task.cancel()")
    drain_at = src.index("coalesce.drain")
    assert cancel_at < drain_at, "stop claiming before draining"
