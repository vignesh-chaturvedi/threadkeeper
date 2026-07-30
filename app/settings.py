"""Central configuration. Every knob in the system is declared here, once.

Nothing reads os.environ directly outside this module. That rule is what makes
`.env.example` an honest document rather than an aspirational one.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


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
        default="postgresql://threadkeeper:dev@localhost:5432/threadkeeper",
        description="psycopg3 async pool + alembic both read this.",
    )
    redis_url: RedisDsn = Field(default="redis://localhost:6379/0")

    db_pool_min: int = 1
    db_pool_max: int = 10

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
