"""Turn coalescing: debounce a burst into one turn, and cancel work it invalidates.

Real people type "hi" / "need loan" / "5 lakh" / "urgent bro" as four separate
messages in six seconds. Handled naively that is four agent runs racing each
other and four replies, which reads as a broken bot.

Two mechanisms, and they are not the same mechanism:

**Debounce** waits for the customer to stop typing. A quiet window of
`buffer_window_s` is extended by each new message, with a hard cap of
`buffer_max_hold_s` measured from the first message so somebody typing
continuously still gets an answer.

**Invalidation** handles the message that lands *after* the window closed and
generation already started. A process-local `asyncio.Task.cancel()` handles the
single-replica case; a monotonic generation counter in Redis handles the case the
plan's note calls out — with two replicas the cancel never reaches the other
process, so every turn re-checks its generation before it is allowed to send. A
reply computed from stale input is dropped rather than delivered.

The ordering that matters: **the buffer is not drained until the reply has
actually shipped.** Drain-then-generate would lose the customer's words whenever
a turn got superseded.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from app.buffer import lock
from app.cache import redis
from app.ingress import outbound, pipeline, repository
from app.ingress.events import InboundEvent, OutboundMessage
from app.logging import get_logger, log_context
from app.settings import get_settings

log = get_logger(__name__)

# Namespaced so a shared Redis stays legible in redis-cli.
K = "tk"


def k_buf(cid: str) -> str:
    return f"{K}:buf:{cid}"


def k_gen(cid: str) -> str:
    return f"{K}:gen:{cid}"


def k_deadline(cid: str) -> str:
    return f"{K}:deadline:{cid}"


def k_first(cid: str) -> str:
    return f"{K}:first:{cid}"


def k_lock(cid: str) -> str:
    return f"{K}:lock:{cid}"


def k_typing(cid: str) -> str:
    return f"{K}:typing:{cid}"


# Strong references to running settle tasks. Without this dict asyncio may
# garbage-collect a task mid-flight, and it is also how we find the task to
# cancel when a newer message arrives in this process.
_inflight: dict[str, asyncio.Task] = {}

_KEY_TTL_S = 300  # buffer keys are transient; never leave them lying around


async def push(evt: InboundEvent) -> None:
    """Record one inbound message and (re)start the settle timer for it.

    Fast by construction: a few Redis writes and one UPDATE. The waiting happens
    in a detached task so the webhook's background task returns immediately.
    """
    cid = evt.conversation_id
    assert cid is not None, "conversation must be resolved before buffering"

    settings = get_settings()
    r = redis()
    now = time.time()

    # INCR first: any turn already in flight is now working from stale input,
    # and this is what tells it so — including on another replica.
    generation = int(await r.incr(k_gen(cid)))
    await r.expire(k_gen(cid), _KEY_TTL_S)

    pipe = r.pipeline()
    pipe.rpush(k_buf(cid), evt.text)
    pipe.expire(k_buf(cid), _KEY_TTL_S)
    pipe.set(k_deadline(cid), now + settings.buffer_window_s, ex=_KEY_TTL_S)
    # NX: the hard cap is measured from the *first* message of the burst, so a
    # steady stream of messages cannot push it back indefinitely.
    pipe.set(k_first(cid), now, nx=True, ex=_KEY_TTL_S)
    pipe.set(k_typing(cid), "1", ex=int(settings.typing_ttl_s))
    await pipe.execute()

    await repository.touch_last_inbound(cid)

    _cancel_local(cid, reason="superseded_by_newer_message")

    task = asyncio.create_task(_settle(cid, generation), name=f"settle:{cid[:8]}:{generation}")
    _inflight[cid] = task
    task.add_done_callback(_discard)

    log.info("buffered", generation=generation, window_s=settings.buffer_window_s)


def _discard(task: asyncio.Task) -> None:
    for cid, running in list(_inflight.items()):
        if running is task:
            _inflight.pop(cid, None)


def _cancel_local(cid: str, *, reason: str) -> None:
    """Cancel this process's in-flight turn for a conversation, if any."""
    task = _inflight.pop(cid, None)
    if task is not None and not task.done():
        task.cancel()
        log.info("turn_cancelled", conversation_id=cid, reason=reason)


async def _wait_for_quiet(cid: str) -> None:
    """Sleep until the customer stops typing, or the hard cap is reached."""
    settings = get_settings()
    r = redis()

    while True:
        now = time.time()
        deadline = float(await r.get(k_deadline(cid)) or now)
        first = float(await r.get(k_first(cid)) or now)

        quiet_in = deadline - now
        capped_in = (first + settings.buffer_max_hold_s) - now
        wait = min(quiet_in, capped_in)

        if wait <= 0:
            if capped_in <= 0 < quiet_in:
                log.info("turn_forced_by_max_hold", max_hold_s=settings.buffer_max_hold_s)
            return

        await asyncio.sleep(wait)


async def _current_generation(cid: str) -> int:
    return int(await redis().get(k_gen(cid)) or 0)


async def _settle(cid: str, my_generation: int) -> None:
    """Wait out the debounce window, then run exactly one turn — or stand down."""
    settings = get_settings()
    r = redis()

    with log_context(conversation_id=cid, generation=my_generation):
        try:
            await _wait_for_quiet(cid)

            # Cheap check before paying for a lock.
            if await _current_generation(cid) != my_generation:
                log.info("turn_abandoned", reason="superseded_before_lock")
                return

            async with lock.guard(k_lock(cid), settings.buffer_lock_ttl_s) as token:
                if token is None:
                    # Another replica owns this conversation. Standing down is
                    # correct; queueing would produce the duplicate reply the
                    # whole phase exists to prevent.
                    log.info("turn_abandoned", reason="conversation_locked_elsewhere")
                    return

                # Re-check under the lock: the generation may have moved while
                # we were waiting to acquire it.
                if await _current_generation(cid) != my_generation:
                    log.info("turn_abandoned", reason="superseded_before_turn")
                    return

                parts = await r.lrange(k_buf(cid), 0, -1)
                if not parts:
                    log.info("turn_abandoned", reason="empty_buffer")
                    return

                turn_text = "\n".join(parts)
                log.info("turn_started", merged_messages=len(parts), chars=len(turn_text))

                # The expensive part. A cancel landing here is the point of the
                # exercise: a half-written reply must never ship.
                reply = await pipeline.compose_reply(cid, turn_text)

                # Final guard, and the one that covers multiple replicas: a
                # message may have arrived while the model was thinking.
                if await _current_generation(cid) != my_generation:
                    log.info("reply_dropped", reason="stale_generation")
                    return

                conversation = await repository.get_conversation(cid)
                await outbound.send(
                    OutboundMessage(
                        channel=conversation["channel"],
                        conversation_id=cid,
                        customer_ref=conversation["customer_ref"],
                        text=reply,
                    )
                )

                # Only now is it safe to forget what the customer said. Trimming
                # exactly what we consumed, rather than DEL, keeps any message
                # that raced in between the read and here.
                await r.ltrim(k_buf(cid), len(parts), -1)
                await r.delete(k_first(cid))
                log.info("turn_completed", merged_messages=len(parts))

        except asyncio.CancelledError:
            # A newer burst owns this conversation now. The buffer is deliberately
            # left intact so the next turn sees everything the customer typed.
            log.info("turn_aborted", reason="cancelled")
            raise
        except Exception:
            log.exception("turn_failed")
        finally:
            # Best effort. This runs during cancellation too, including at
            # shutdown when the Redis pool may already be closed — and an
            # exception raised from a finally block would mask the CancelledError
            # that is propagating through it.
            with contextlib.suppress(Exception):
                if await _current_generation(cid) == my_generation:
                    await r.delete(k_typing(cid))


async def is_typing(cid: str) -> bool:
    return bool(await redis().exists(k_typing(cid)))


async def pending(cid: str) -> dict[str, object]:
    """Buffer state, for the simulator's inspector. Read-only."""
    r = redis()
    parts = await r.lrange(k_buf(cid), 0, -1)
    deadline = await r.get(k_deadline(cid))
    return {
        "queued": len(parts),
        "messages": parts,
        "generation": await _current_generation(cid),
        "closes_in_s": max(0.0, round(float(deadline) - time.time(), 2)) if deadline else 0.0,
        "typing": bool(await r.exists(k_typing(cid))),
        "locked": bool(await r.exists(k_lock(cid))),
    }


async def drain(timeout_s: float) -> dict[str, int]:
    """Let in-flight turns finish. Cancel only what is still running after that.

    This is the difference between a deploy nobody notices and a deploy that
    eats replies. A settle task is a customer's turn: their message is already
    buffered in Redis and their reply has not been sent. Cancelling it — which
    is what this function used to do unconditionally — means the message is
    consumed and nothing ever comes back.

    Cancellation is still correct in the one case it was designed for: a *newer*
    message supersedes the turn in flight, and the customer gets a reply to the
    newer one. A SIGTERM is not a newer message.

    Bounded, because a hung turn must not hold the container open until the
    orchestrator escalates to SIGKILL — which would cancel everything anyway,
    with no log line explaining it. Waiting a known number of seconds and then
    saying what was abandoned is strictly better than being killed.
    """
    tasks = [t for t in _inflight.values() if not t.done()]
    if not tasks:
        log.info("buffer_drained", finished=0, cancelled=0)
        return {"finished": 0, "cancelled": 0}

    log.info("buffer_draining", in_flight=len(tasks), timeout_s=timeout_s)
    done, pending = await asyncio.wait(tasks, timeout=timeout_s)

    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    _inflight.clear()
    log.info("buffer_drained", finished=len(done), cancelled=len(pending))
    return {"finished": len(done), "cancelled": len(pending)}


async def shutdown() -> None:
    """Immediate stop, no grace. Tests and local Ctrl-C; never a deploy."""
    await drain(timeout_s=0)
