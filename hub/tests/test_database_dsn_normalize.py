"""Unit tests for hub/db/database.py::_normalize_async_dsn.

The shared DATABASE_URL secret is set with `postgresql://...` for
compatibility with psycopg2-based tooling (migrations, ad-hoc psql,
legacy engine services). The hub however uses SQLAlchemy
`create_async_engine` which only accepts the `postgresql+asyncpg://`
dialect prefix. Without a normalisation step SQLAlchemy routes to
the sync psycopg2 dialect and the hub container crashes at startup
with `ModuleNotFoundError: No module named 'psycopg2'` (the hub has
asyncpg but deliberately does NOT pin psycopg2).

Discovered during the DEP-02 AWS hub migration on 2026-04-11.
"""
from __future__ import annotations

import pytest

from hub.db.database import _normalize_async_dsn, _redact_dsn


class TestNormalizeAsyncDsn:
    """The normaliser must canonicalise every common dialect prefix
    to `postgresql+asyncpg://`, and leave invalid / empty input alone."""

    def test_postgresql_scheme_gets_asyncpg_dialect(self):
        raw = "postgresql://trader:trader@db.example.com:5432/trader"
        expected = "postgresql+asyncpg://trader:trader@db.example.com:5432/trader"
        assert _normalize_async_dsn(raw) == expected

    def test_postgres_scheme_gets_asyncpg_dialect(self):
        """Short alias `postgres://` is canonicalised the same way."""
        raw = "postgres://user:pw@host/dbname"
        expected = "postgresql+asyncpg://user:pw@host/dbname"
        assert _normalize_async_dsn(raw) == expected

    def test_already_asyncpg_is_noop(self):
        raw = "postgresql+asyncpg://trader:trader@db:5432/trader"
        assert _normalize_async_dsn(raw) == raw

    def test_psycopg2_suffix_is_replaced_with_asyncpg(self):
        raw = "postgresql+psycopg2://trader@db:5432/trader"
        expected = "postgresql+asyncpg://trader@db:5432/trader"
        assert _normalize_async_dsn(raw) == expected

    def test_psycopg_v3_suffix_is_replaced_with_asyncpg(self):
        raw = "postgresql+psycopg://trader@db:5432/trader"
        expected = "postgresql+asyncpg://trader@db:5432/trader"
        assert _normalize_async_dsn(raw) == expected

    def test_postgres_plus_psycopg2_is_replaced(self):
        raw = "postgres+psycopg2://trader@db:5432/trader"
        expected = "postgresql+asyncpg://trader@db:5432/trader"
        assert _normalize_async_dsn(raw) == expected

    def test_empty_string_is_noop(self):
        assert _normalize_async_dsn("") == ""

    def test_query_parameters_preserved(self):
        """sslmode=require and similar params survive normalisation."""
        raw = "postgresql://user:pw@host/db?sslmode=require&connect_timeout=10"
        normalized = _normalize_async_dsn(raw)
        assert normalized.startswith("postgresql+asyncpg://")
        assert "sslmode=require" in normalized
        assert "connect_timeout=10" in normalized

    def test_non_postgres_url_is_left_alone(self):
        """Defensive: if someone sets DATABASE_URL to something weird
        (mysql, sqlite, etc.) we leave it alone so SQLAlchemy can surface
        a clean error rather than us silently mangling it."""
        raw = "mysql://user:pw@host/db"
        assert _normalize_async_dsn(raw) == raw


class TestRedactDsn:
    """The startup log line must never contain raw credentials."""

    def test_redacts_user_and_password(self):
        raw = "postgresql+asyncpg://trader:supersecret@db.example.com:5432/trader"
        redacted = _redact_dsn(raw)
        assert "trader" not in redacted or "***" in redacted
        assert "supersecret" not in redacted
        assert "@db.example.com:5432/trader" in redacted

    def test_dsn_without_credentials_is_noop(self):
        raw = "postgresql+asyncpg://db.example.com/trader"
        # No `@` → nothing to redact
        assert _redact_dsn(raw) == raw

    def test_empty_string_safe(self):
        assert _redact_dsn("") == ""

    def test_no_scheme_safe(self):
        # Pathological input — still must not crash
        assert _redact_dsn("user:pw@host") == "user:pw@host"
