"""The model seam.

Two methods, deliberately, because Phase 03's key decision is that they are two
different calls:

  * `extract()` returns structured data against a JSON schema.
  * `reply()` returns prose.

Mixing them — asking one call to both fill slots and write the customer-facing
message — makes both worse and neither testable. Extraction can then be scored
against a labelled set (Phase 09) without a human reading anything, and the
reply prompt can change without silently altering what the system believes.

Every provider reports token usage, because "cost per conversation" is a metric
this project has to be able to answer (Phase 10), and retrofitting accounting is
worse than carrying it from the start.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            calls=self.calls + other.calls,
        )


@dataclass(slots=True)
class Extraction:
    data: dict[str, Any] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)


@dataclass(slots=True)
class Embedding:
    vector: list[float] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


@dataclass(slots=True)
class Reply:
    text: str = ""
    usage: Usage = field(default_factory=Usage)


class ModelError(Exception):
    """Provider failed. The caller degrades gracefully rather than crashing a turn."""


@runtime_checkable
class ModelProvider(Protocol):
    name: str

    async def extract(self, *, system: str, user: str, schema: dict[str, Any]) -> Extraction: ...

    async def reply(self, *, system: str, user: str, history: list[dict[str, str]]) -> Reply: ...

    async def embed(self, *, text: str) -> Embedding: ...
