"""URL normalization for Railway's Postgres injection."""

from app.db import _normalize_db_url


def test_postgres_scheme_normalized() -> None:
    assert _normalize_db_url("postgres://user:pw@host/db") == "postgresql+asyncpg://user:pw@host/db"


def test_postgresql_scheme_normalized() -> None:
    assert _normalize_db_url("postgresql://user:pw@host/db") == "postgresql+asyncpg://user:pw@host/db"


def test_already_asyncpg_untouched() -> None:
    url = "postgresql+asyncpg://user:pw@host/db"
    assert _normalize_db_url(url) == url


def test_sqlite_untouched() -> None:
    url = "sqlite+aiosqlite:///./cutebot.db"
    assert _normalize_db_url(url) == url
