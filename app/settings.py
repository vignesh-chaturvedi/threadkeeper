"""Central configuration. Every knob in the system is declared here, once.

Nothing reads os.environ directly outside this module. That rule is what makes
`.env.example` an honest document rather than an aspirational one.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The sentinel that makes "did anyone actually configure this?" answerable.
# Not a credential — it exists so that shipping the unconfigured default is a
# startup failure rather than a silent, guessable-customer_ref deployment.
DEV_REF_SECRET_SENTINEL = "dev-only-customer-ref-secret"  # noqa: S105
# The same idea for the vault: a shipped default key is not a key.
DEV_VAULT_KEY = "dGhyZWFka2VlcGVyLWxvY2FsLWRldi12YXVsdC1rZXk="


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="TK_",
        extra="ignore",
    )

    # --- runtime -----------------------------------------------------------
    env: Literal["local", "test", "staging", "prod"] = "local"
    debug: bool = False
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # --- stores ------------------------------------------------------------
    database_url: PostgresDsn = Field(
        default="postgresql://threadkeeper:dev@localhost:5433/threadkeeper",
        description="psycopg3 async pool + alembic both read this.",
    )
    redis_url: RedisDsn = Field(default="redis://localhost:6379/0")

    db_pool_min: int = 1
    db_pool_max: int = 10

    # --- channel: WhatsApp -------------------------------------------------
    whatsapp_app_secret: str = Field(
        default="",
        description="BSP app secret used for X-Hub-Signature-256 HMAC verification.",
    )
    whatsapp_verify_token: str = Field(
        default="threadkeeper-dev-verify",
        description="Echoed during Meta's GET webhook subscription handshake.",
    )
    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: str = ""
    whatsapp_api_base: str = "https://graph.facebook.com/v21.0"

    # --- outbound ----------------------------------------------------------
    outbound_transport: Literal["mock", "whatsapp"] = "mock"
    outbound_max_attempts: int = 4
    outbound_backoff_s: tuple[float, ...] = (0.5, 2.0, 5.0)
    outbound_failure_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Mock transport only. Injects transient failures so retry paths get exercised.",
    )

    # --- turn coalescing ---------------------------------------------------
    buffer_window_s: float = Field(
        default=2.5,
        description="Quiet period before a burst is treated as one turn. Extended by each message.",
    )
    buffer_max_hold_s: float = Field(
        default=8.0,
        description=(
            "Hard cap measured from the first message of a burst, so a chatty "
            "user cannot defer a reply forever."
        ),
    )
    buffer_lock_ttl_s: float = 30.0
    typing_ttl_s: float = Field(
        default=12.0,
        description="TTL on the typing flag, so a crashed worker doesn't leave it stuck on.",
    )
    fake_turn_latency_s: float = Field(
        default=0.0,
        description=(
            "Dev affordance: pretend the model takes this long, so in-flight "
            "cancellation is observable in the simulator. Never set outside local."
        ),
    )

    # --- model -------------------------------------------------------------
    llm_provider: Literal["gemini", "fake"] = Field(
        default="fake",
        description="'fake' is deterministic and offline — the default so tests never spend money.",
    )
    gemini_api_key: str = ""
    gemini_api_base: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_reply_model: str = "gemini-3.5-flash-lite"
    gemini_extract_model: str = Field(
        default="gemini-3.5-flash-lite",
        description=(
            "Extraction may use a smaller model than the reply. Separate knobs so that "
            "choice can be measured rather than assumed."
        ),
    )
    llm_timeout_s: float = 20.0
    llm_max_attempts: int = 3
    llm_max_output_tokens: int = 320

    # --- lender tools ------------------------------------------------------
    lender_latency_ms: int = Field(
        default=0,
        description="Simulated lender latency. Turn up to see the agent handle a slow API.",
    )
    lender_failure_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Fraction of lender calls that fail. The plan asks for ~0.05 — an agent "
            "whose lender never times out has a degradation path that never runs."
        ),
    )

    # --- memory ------------------------------------------------------------
    working_budget_tokens: int = Field(
        default=1200,
        description=(
            "Tier 1 budget for recent turns. Trimmed by estimated tokens, not "
            "message count — see app/memory/tokens.py for the calibration."
        ),
    )
    enable_semantic_memory: bool = Field(
        default=True,
        description="Tier 3. Turned off by evals/memory_ab.py to measure what it is worth.",
    )
    recall_top_k: int = 2
    embedding_model: str = "gemini-embedding-2"
    embedding_dimensions: int = Field(
        default=768,
        description=(
            "A schema decision, not a knob: the pgvector column is vector(768) "
            "and changing this needs a migration."
        ),
    )

    # --- funnel ------------------------------------------------------------
    history_turns: int = Field(
        default=10,
        description=(
            "Messages of context passed to the reply call. Phase 04 replaces this "
            "count with a token budget."
        ),
    )

    # --- privacy -----------------------------------------------------------
    customer_ref_secret: str = Field(
        default=DEV_REF_SECRET_SENTINEL,
        description="HMAC key for deriving customer_ref from a phone number.",
    )

    # --- privacy: the vault ------------------------------------------------
    vault_key: str = Field(
        # Rotating this makes every existing token undecryptable, which is why
        # key rotation is a migration, not a config change.
        default=DEV_VAULT_KEY,
        description="Fernet key for the PII vault. Must be set outside local/test.",
    )
    tokenize_inbound: bool = Field(
        default=True,
        description=(
            "Tokenize identifiers before the message is stored at all, not just "
            "before the model call. Off only for debugging."
        ),
    )

    # --- local tooling -----------------------------------------------------
    enable_simulator: bool = Field(
        default=True,
        description="Mounts /sim, the fake WhatsApp client. Forced off outside local/test.",
    )

    @model_validator(mode="after")
    def _refuse_unsafe_production_config(self) -> Settings:
        """Fail at startup rather than at 3am.

        A missing app secret means the webhook accepts unsigned payloads, and a
        default HMAC key means customer_refs are guessable. Both are acceptable
        locally and neither is acceptable deployed.
        """
        if self.env in ("staging", "prod"):
            if not self.whatsapp_app_secret:
                raise ValueError("TK_WHATSAPP_APP_SECRET is required outside local/test")
            if self.customer_ref_secret == DEV_REF_SECRET_SENTINEL:
                raise ValueError("TK_CUSTOMER_REF_SECRET must be set outside local/test")
            if self.vault_key == DEV_VAULT_KEY:
                raise ValueError("TK_VAULT_KEY must be set outside local/test")
            object.__setattr__(self, "enable_simulator", False)
        return self

    @property
    def verify_signatures(self) -> bool:
        """No secret configured locally means no signature to check against."""
        return bool(self.whatsapp_app_secret)

    @property
    def alembic_url(self) -> str:
        """SQLAlchemy needs the driver named explicitly; psycopg3 is the only one we ship."""
        return str(self.database_url).replace("postgresql://", "postgresql+psycopg://", 1)

    @property
    def psycopg_url(self) -> str:
        """Raw libpq URL for psycopg / the LangGraph checkpointer."""
        return str(self.database_url)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
