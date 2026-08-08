"""Gemini provider, over the REST API with httpx.

Not the vendor SDK, on purpose. The surface this project needs is two endpoints
and a JSON schema; against that, an SDK buys little and costs a dependency that
pins its own httpx and protobuf versions alongside LangGraph's. Writing the ~120
lines means the retry policy, the timeout, and the token accounting are all
things I can explain rather than things that happen somewhere in a vendor's
call stack.

Note `responseSchema` uses Gemini's own type spellings (STRING/INTEGER/OBJECT),
not JSON Schema's lowercase ones — a detail that fails silently by returning
unconstrained prose if you get it wrong.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from contextvars import ContextVar
from typing import Any

import httpx

from app.llm.base import Embedding, Extraction, ModelError, Reply, Usage
from app.llm.keypool import KeyPool
from app.logging import get_logger
from app.settings import get_settings

log = get_logger(__name__)

_RETRYABLE = {408, 429, 500, 502, 503, 504}


# Per-turn throttle accounting, held in a *mutable* box on purpose.
#
# A ContextVar holding an int does not work here: the graph runs its nodes in
# child tasks, and a task inherits a *copy* of the context, so a `.set()` inside
# a node is invisible to the caller that started the turn. Every wait was being
# recorded and then discarded, and the console went on reporting 50-second turns.
# Storing one dict and mutating it in place shares the object across those
# copies, which is the behaviour actually wanted.
_throttle: ContextVar[dict[str, int]] = ContextVar("llm_throttle")


def begin_turn() -> None:
    """Start a fresh throttle account. Called once per turn, by the runner."""
    _throttle.set({"ms": 0})


def throttled_ms() -> int:
    """Milliseconds spent waiting on our own rate limiter since `begin_turn`."""
    try:
        return _throttle.get()["ms"]
    except LookupError:
        # Nobody opened an account — a direct provider call outside a turn.
        return 0


def _record_wait(seconds: float) -> None:
    """Charge our own pacing to the turn's throttle account, not to the model.

    Without this the console reported a 50-second turn at consent, 47 seconds of
    which was this project's own throttle — and "which stage is slow" became
    unanswerable.
    """
    if seconds <= 0:
        return
    try:
        _throttle.get()["ms"] += int(seconds * 1000)
    except LookupError:
        # Nobody opened an account — a direct provider call outside a turn.
        pass


_pool: KeyPool | None = None
_pool_keys: tuple[str, ...] = ()


def get_pool() -> KeyPool:
    """The process-wide pool, rebuilt only when the configured keys change.

    Module-level rather than per-provider: two provider instances sharing a key
    would otherwise each believe they had the whole limit, and together spend
    twice it.
    """
    global _pool, _pool_keys
    keys = tuple(get_settings().gemini_key_pool)
    if _pool is None or keys != _pool_keys:
        _pool, _pool_keys = KeyPool(list(keys)), keys
        if keys:
            log.info("llm_key_pool", size=len(keys), fingerprints=_pool.fingerprints)
    return _pool


class GeminiProvider:
    name = "gemini"

    def __init__(self) -> None:
        settings = get_settings()
        self._base = settings.gemini_api_base
        self._reply_model = settings.gemini_reply_model
        self._extract_model = settings.gemini_extract_model
        self._timeout = settings.llm_timeout_s
        self._max_attempts = settings.llm_max_attempts

    # ------------------------------------------------------------------ http
    async def _post(
        self, model: str, body: dict[str, Any], endpoint: str = "generateContent"
    ) -> dict[str, Any]:
        pool, settings = get_pool(), get_settings()
        if not len(pool):
            raise ModelError("no Gemini key configured — set TK_GEMINI_API_KEY")

        url = f"{self._base}/models/{model}:{endpoint}"
        last: str = "unknown"

        for attempt in range(1, self._max_attempts + 1):
            try:
                lease, waited = await pool.acquire(
                    max_rpm=settings.llm_max_rpm, max_rpd=settings.llm_max_rpd
                )
            except RuntimeError as exc:  # every key spent its daily quota
                raise ModelError(str(exc)) from exc
            _record_wait(waited)

            headers = {"x-goog-api-key": lease.key, "content-type": "application/json"}
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(url, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                last = f"timeout: {exc}"
            except httpx.HTTPError as exc:
                last = f"transport: {exc}"
            else:
                if resp.status_code == 200:
                    return resp.json()
                last = f"{resp.status_code}: {resp.text[:200]}"
                if resp.status_code == 429:
                    # This key is refusing, not the provider as a whole. Bench it
                    # and let the next attempt land on a different one — with a
                    # pool, a 429 is a routing event rather than a delay.
                    pool.penalise(lease, settings.llm_key_cooldown_s)
                elif resp.status_code not in _RETRYABLE:
                    raise ModelError(last)

            if attempt < self._max_attempts:
                # Full jitter, same reasoning as the outbound sender: a provider
                # blip must not turn into a synchronised retry storm.
                delay = min(2.0**attempt, 8.0) * (secrets.randbelow(1000) / 1000)
                log.warning(
                    "llm_retry",
                    model=model,
                    attempt=attempt,
                    error=last,
                    key_index=lease.index,
                    key_fp=lease.fp,
                )
                await asyncio.sleep(delay)
                _record_wait(delay)

        raise ModelError(f"gemini failed after {self._max_attempts} attempts — {last}")

    @staticmethod
    def _usage(payload: dict[str, Any]) -> Usage:
        meta = payload.get("usageMetadata") or {}
        return Usage(
            input_tokens=int(meta.get("promptTokenCount") or 0),
            output_tokens=int(meta.get("candidatesTokenCount") or 0),
            calls=1,
        )

    @staticmethod
    def _text(payload: dict[str, Any]) -> str:
        candidates = payload.get("candidates") or []
        if not candidates:
            # Safety blocks land here. Treat as an error so the caller degrades
            # deliberately rather than sending an empty message.
            raise ModelError(f"no candidates: {json.dumps(payload)[:200]}")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts).strip()

    # --------------------------------------------------------------- extract
    async def extract(self, *, system: str, user: str, schema: dict[str, Any]) -> Extraction:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
                "temperature": 0.0,  # extraction is not a creative task
            },
        }
        payload = await self._post(self._extract_model, body)
        raw = self._text(payload)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ModelError(f"extraction was not valid json: {raw[:200]}") from exc
        if not isinstance(data, dict):
            raise ModelError(f"extraction was not an object: {raw[:120]}")
        return Extraction(data=data, usage=self._usage(payload))

    # ------------------------------------------------------------- embedding
    async def embed(self, *, text: str) -> Embedding:
        settings = get_settings()
        body = {
            "content": {"parts": [{"text": text}]},
            # Pinned: the column type is vector(N) and N cannot change without a
            # migration, so the dimensionality is a schema decision, not a knob.
            "outputDimensionality": settings.embedding_dimensions,
        }
        payload = await self._post(settings.embedding_model, body, endpoint="embedContent")
        values = (payload.get("embedding") or {}).get("values") or []
        if len(values) != settings.embedding_dimensions:
            raise ModelError(f"expected {settings.embedding_dimensions} dims, got {len(values)}")
        return Embedding(vector=[float(v) for v in values], usage=Usage(calls=1))

    # ----------------------------------------------------------------- reply
    async def reply(self, *, system: str, user: str, history: list[dict[str, str]]) -> Reply:
        contents: list[dict[str, Any]] = [
            {"role": "model" if h["role"] == "agent" else "user", "parts": [{"text": h["text"]}]}
            for h in history
            if h.get("text")
        ]
        contents.append({"role": "user", "parts": [{"text": user}]})

        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {
                "temperature": 0.4,
                "maxOutputTokens": get_settings().llm_max_output_tokens,
            },
        }
        payload = await self._post(self._reply_model, body)
        return Reply(text=self._text(payload), usage=self._usage(payload))
