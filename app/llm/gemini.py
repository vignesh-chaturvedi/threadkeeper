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
from typing import Any

import httpx

from app.llm.base import Extraction, ModelError, Reply, Usage
from app.logging import get_logger
from app.settings import get_settings

log = get_logger(__name__)

_RETRYABLE = {408, 429, 500, 502, 503, 504}


class GeminiProvider:
    name = "gemini"

    def __init__(self) -> None:
        settings = get_settings()
        self._key = settings.gemini_api_key
        self._base = settings.gemini_api_base
        self._reply_model = settings.gemini_reply_model
        self._extract_model = settings.gemini_extract_model
        self._timeout = settings.llm_timeout_s
        self._max_attempts = settings.llm_max_attempts

    # ------------------------------------------------------------------ http
    async def _post(self, model: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self._key:
            raise ModelError("TK_GEMINI_API_KEY is not set")

        url = f"{self._base}/models/{model}:generateContent"
        headers = {"x-goog-api-key": self._key, "content-type": "application/json"}
        last: str = "unknown"

        for attempt in range(1, self._max_attempts + 1):
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
                if resp.status_code not in _RETRYABLE:
                    raise ModelError(last)

            if attempt < self._max_attempts:
                # Full jitter, same reasoning as the outbound sender: a provider
                # blip must not turn into a synchronised retry storm.
                delay = min(2.0**attempt, 8.0) * (secrets.randbelow(1000) / 1000)
                log.warning("llm_retry", model=model, attempt=attempt, error=last)
                await asyncio.sleep(delay)

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
