"""Model provider registry. One switch, set by TK_LLM_PROVIDER."""

from __future__ import annotations

from functools import lru_cache

from app.llm.base import Embedding, Extraction, ModelError, ModelProvider, Reply, Usage
from app.llm.fake import FakeProvider
from app.llm.gemini import GeminiProvider
from app.logging import get_logger
from app.settings import get_settings

log = get_logger(__name__)


@lru_cache(maxsize=1)
def get_provider() -> ModelProvider:
    """Chosen once per process.

    Falls back to the fake when the real provider has no key rather than
    failing at the first turn: a missing key should degrade the demo, not take
    the service down.
    """
    settings = get_settings()
    if settings.llm_provider == "gemini":
        if settings.gemini_key_pool:
            return GeminiProvider()
        log.warning("llm_provider_fallback", requested="gemini", reason="no_api_key")
    return FakeProvider()


__all__ = [
    "Embedding",
    "Extraction",
    "ModelError",
    "ModelProvider",
    "Reply",
    "Usage",
    "get_provider",
]
