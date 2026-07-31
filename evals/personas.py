"""Simulated customers.

Each persona is a YAML file carrying two ways to be played:

  * a **system prompt**, when a model is driving — realistic, varied, and what
    the numbers in the report are produced with;
  * a **script**, when the provider is `fake` — deterministic, free, offline, so
    CI can run the whole suite on every PR without a key or a bill.

Both matter. The scripted mode is what makes "green on every PR" affordable; the
model-driven mode is what makes the scorecard mean something. A harness that only
had the first would be measuring my own regexes.

The plan says to fix the seed so runs are comparable. Personas run at
temperature 0 and the scripts are ordered, so a rerun against unchanged code
produces the same conversations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.llm import ModelError, get_provider
from app.logging import get_logger
from app.settings import get_settings

log = get_logger(__name__)

PERSONA_DIR = Path(__file__).parent / "personas"

# The signal a persona is finished. A ghoster reaches it by running out of
# script; the others say it.
DONE = "DONE"


@dataclass(slots=True)
class Persona:
    name: str
    label: str
    goal: str
    system: str
    script: list[str]
    max_turns: int = 12
    expects: dict[str, Any] = field(default_factory=dict)

    @property
    def must_not(self) -> list[str]:
        return list(self.expects.get("must_not") or [])


def load_all() -> list[Persona]:
    personas: list[Persona] = []
    for path in sorted(PERSONA_DIR.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        personas.append(
            Persona(
                name=raw["name"],
                label=raw["label"],
                goal=raw.get("goal", "").strip(),
                system=raw.get("system", "").strip(),
                script=list(raw.get("script") or []),
                max_turns=int(raw.get("max_turns", 12)),
                expects=raw.get("expects") or {},
            )
        )
    return personas


def load(name: str) -> Persona:
    for persona in load_all():
        if persona.name == name:
            return persona
    raise KeyError(f"no persona named {name!r}")


async def next_message(persona: Persona, transcript: list[tuple[str, str]]) -> str | None:
    """What the customer says next, or None if they are finished.

    None is not an error — a ghoster going quiet is the behaviour being tested,
    and the scheduler has something real to act on precisely because of it.
    """
    settings = get_settings()
    turn = len([1 for who, _ in transcript if who == "customer"])

    # --- scripted mode: deterministic, free -----------------------------
    if settings.llm_provider == "fake" or not persona.system:
        if turn >= len(persona.script):
            return None
        return persona.script[turn]

    # --- model-driven mode ----------------------------------------------
    if turn >= persona.max_turns:
        return None

    history = [
        {"role": "agent" if who == "customer" else "customer", "text": text}
        for who, text in transcript
    ]
    try:
        # The persona plays the customer, so the roles are inverted relative to
        # the agent's own view: what the agent said is the "user" turn here.
        result = await get_provider().reply(
            system=persona.system,
            user=(
                transcript[-1][1]
                if transcript
                else "(the conversation has not started — send your first message)"
            ),
            history=history,
        )
    except ModelError as exc:
        log.warning("persona_failed", persona=persona.name, error=str(exc))
        # Fall back to the script rather than abandoning the run.
        return persona.script[turn] if turn < len(persona.script) else None

    text = result.text.strip()
    if not text or text.upper().startswith(DONE):
        return None
    return text
