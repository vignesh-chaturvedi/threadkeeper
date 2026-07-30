"""Guards on the two pieces of Phase 00 that are easy to get subtly wrong."""

from __future__ import annotations

import io
import json

import pytest
import structlog

from app.logging import configure_logging, log_context
from app.settings import Settings


def test_alembic_url_names_the_psycopg_driver() -> None:
    """SQLAlchemy defaults to psycopg2, which this project does not install."""
    s = Settings(database_url="postgresql://u:p@h:5432/db")
    assert s.alembic_url == "postgresql+psycopg://u:p@h:5432/db"
    # The raw libpq URL must stay driverless for psycopg / the LangGraph saver.
    assert s.psycopg_url == "postgresql://u:p@h:5432/db"


def test_settings_read_the_tk_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TK_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("TK_ENV", "test")
    s = Settings(_env_file=None)
    assert s.log_level == "DEBUG"
    assert s.env == "test"


def test_conversation_id_lands_on_every_line_in_context() -> None:
    """The whole reason this module exists: bind once, appears everywhere."""
    buf = io.StringIO()
    configure_logging(force=True)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=buf),
        cache_logger_on_first_use=False,
    )
    log = structlog.get_logger("t")

    with log_context(conversation_id="conv-42"):
        log.info("inside_a")
        log.info("inside_b", extra="x")
    log.info("outside")

    lines = [json.loads(line) for line in buf.getvalue().strip().splitlines()]
    assert lines[0]["conversation_id"] == "conv-42"
    assert lines[1]["conversation_id"] == "conv-42"
    assert "conversation_id" not in lines[2], "context must not leak past the block"
