"""Measure the token estimator against the real tokenizer.

Kept in the repo because the constants in `app/memory/tokens.py` are otherwise
just numbers someone picked. Re-run it when the model changes:

    uv run python -m evals.calibrate_tokens

Costs nothing — `countTokens` is not billed — but it does need a network and a
key, which is why it is a script rather than a test.

The findings that shaped the estimator, from the run on 31 Jul 2026 against
gemini-3.5-flash-lite:

  * Devanagari is *not* denser than English (4.69 vs 4.40 chars/token). I
    assumed the opposite before measuring, and would have over-trimmed every
    Hindi conversation.
  * Punctuation-dense text is much denser: a JSON-shaped profile block runs at
    2.33 chars/token, so `len // 4` under-counts precisely the structured block
    injected into every prompt. That is why rendering the profile as lines
    rather than JSON is a token decision, not a cosmetic one.
  * Romanised Hindi costs more than English per character (3.35), because the
    words are not in the vocabulary.
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from app.memory.tokens import estimate_tokens
from app.settings import get_settings

SAMPLES = [
    ("english-short", "I need a personal loan"),
    ("english-tiny", "hi"),
    ("hinglish-tiny", "haan theek hai"),
    (
        "english-long",
        "I am looking for a personal loan of about five lakh rupees to consolidate "
        "some existing debt and would like to know what options are available.",
    ),
    (
        "hinglish",
        "bhai mujhe 5 lakh ka personal loan chahiye, salary 60k hai, PAN nahi hai abhi",
    ),
    ("devanagari", "मुझे पाँच लाख रुपये का व्यक्तिगत ऋण चाहिए, ब्याज दर क्या होगी"),
    ("code-mixed", "PAN nahi hai अभी, but salary 60k hai — EMI kitni banegi?"),
    ("json", '{"product":"personal_loan","amount_inr":500000,"income_band":"50k_1l"}'),
    (
        "profile-block",
        "- wants: personal loan\n- amount: 5 lakh\n- income: 50k-1L/month\n- PAN: available",
    ),
    (
        "consent-wording",
        "To check which lenders you qualify for, I need your permission to share "
        "the details you have given me with our partner lenders.",
    ),
    ("objection", "interest rate kitna hai? processing fees bhi lagenge kya?"),
    ("off-topic", "aaj mausam accha hai na"),
    ("opt-out", "stop. band karo. mat bhejo mujhe message"),
]


async def count_real(client: httpx.AsyncClient, model: str, key: str, text: str) -> int:
    resp = await client.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:countTokens",
        headers={"x-goog-api-key": key, "content-type": "application/json"},
        json={"contents": [{"parts": [{"text": text}]}]},
    )
    resp.raise_for_status()
    return int(resp.json()["totalTokens"])


async def main() -> int:
    settings = get_settings()
    if not settings.gemini_api_key:
        print("TK_GEMINI_API_KEY is not set — nothing to calibrate against.")
        return 2

    model = settings.gemini_reply_model
    print(f"calibrating app/memory/tokens.py against {model}\n")
    print(f"{'sample':18} {'chars':>6} {'real':>6} {'est':>6} {'err':>8}  {'c/tok':>6}")

    worst_under = 0.0
    errors: list[float] = []

    async with httpx.AsyncClient(timeout=20) as client:
        for name, text in SAMPLES:
            real = await count_real(client, model, settings.gemini_api_key, text)
            est = estimate_tokens(text)
            err = (est - real) / real * 100
            worst_under = min(worst_under, err)
            errors.append(err)
            print(f"{name:18} {len(text):6} {real:6} {est:6} {err:+7.1f}%  {len(text) / real:6.2f}")

    mean = sum(errors) / len(errors)
    print(f"\n  samples:            {len(SAMPLES)}")
    print(f"  worst UNDER-estimate: {worst_under:+.1f}%   (must be >= 0)")
    print(f"  mean over-estimate:   {mean:+.1f}%")
    print(f"  verdict: {'SAFE' if worst_under >= 0 else 'UNSAFE — the budget can overflow'}")
    return 0 if worst_under >= 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
