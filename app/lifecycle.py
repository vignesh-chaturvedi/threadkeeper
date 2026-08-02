"""Process lifecycle: are we starting, serving, or going away?

One flag, read by the readiness probe and set by the shutdown path. It exists
because "is the process alive" and "should this process receive new work" are
different questions, and a deploy is the moment the difference matters.

The ordering a rolling deploy actually produces:

    1. the orchestrator decides to replace this task
    2. it deregisters the target and waits out the deregistration delay,
       during which the load balancer stops sending new requests
    3. SIGTERM
    4. in-flight turns finish            <- app/buffer/coalesce.drain()
    5. SIGKILL, after stopTimeout

Step 4 is the one worth building. Steps 2 and 5 are the orchestrator's, and the
only thing this code owes them is to finish inside the window they allow —
which is why `TK_DRAIN_TIMEOUT_S` and the Terraform `stop_timeout` are two
numbers that have to be read together, and are commented as such in both places.

Readiness reporting `draining` is not what stops the traffic on ECS — the load
balancer has already stopped it by step 3. It matters on Kubernetes, where the
endpoint is removed in response to the probe rather than before the signal, and
it makes the state visible to anyone looking at the container while it exits.
"""

from __future__ import annotations

import time

from app.logging import get_logger

log = get_logger(__name__)

STARTED_AT = time.time()

_draining = False


def begin_drain() -> None:
    global _draining
    if not _draining:
        _draining = True
        log.info("draining_started", uptime_s=round(uptime(), 1))


def is_draining() -> bool:
    return _draining


def uptime() -> float:
    return time.time() - STARTED_AT


def reset_for_tests() -> None:
    """Module state is process-global; a test that drains would poison the rest."""
    global _draining
    _draining = False
