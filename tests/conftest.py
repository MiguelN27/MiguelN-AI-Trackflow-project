"""Test fixtures shared by every suite.

Every test runs against its own TinyDB file in a temporary directory. The
handle is cached with `lru_cache`, so redirecting `DB_PATH` is not enough on its
own - the cache has to be cleared as well, before and after, or one test's
incidents leak into the next and into `data/suppliers.json`.
"""

import pytest
from fastapi.testclient import TestClient

from services.core import config
from services.core import db as core_db
from services.inventory import database as inventory_database
from services.main import app

# Settings every test reads, whatever the developer's `.env` holds. Environment
# variables outrank `.env` in pydantic-settings, so these win.
TEST_SETTINGS = {
    "JWT_SECRET_KEY": "test-only-secret-key-that-is-at-least-32-chars",
    "APP_ENV": "production",
    "DATABASE_URL": "postgresql://test:test@127.0.0.1:1/trackflow_test",
    "JWT_ALGORITHM": "HS256",
    "ACCESS_TOKEN_EXPIRE_MINUTES": "60",
    "PASSWORD_RESET_TOKEN_EXPIRE_MINUTES": "30",
    # Empty selects the console backend: no test can send a real email, even on
    # a machine whose `.env` carries a live Resend key.
    "RESEND_API_KEY": "",
    "EMAIL_FROM": "TrackFlow <onboarding@resend.dev>",
    "FRONTEND_BASE_URL": "http://localhost:3000",
}


@pytest.fixture(autouse=True)
def pinned_settings(monkeypatch):
    """Pin the settings, so the suite needs no `.env` and runs the same everywhere.

    `get_settings` is cached, so the cache is cleared on the way in and out;
    a test that overrides one variable clears it again itself.
    """
    for name, value in TEST_SETTINGS.items():
        monkeypatch.setenv(name, value)
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def isolated_postgres(monkeypatch):
    import psycopg2

    engine_factory = inventory_database.get_engine
    dispose = inventory_database.dispose_engine

    def forbid_connection(*args, **kwargs):
        raise AssertionError("Unit tests must not connect to PostgreSQL")

    dispose()
    monkeypatch.setattr(psycopg2, "connect", forbid_connection)
    monkeypatch.setattr(inventory_database, "check_connection", lambda: None)
    monkeypatch.setattr(inventory_database, "initialize_schema", lambda: None)
    yield
    monkeypatch.setattr(inventory_database, "get_engine", engine_factory)
    dispose()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "incidents.json"
    monkeypatch.setenv("DB_PATH", str(path))
    core_db.get_db.cache_clear()
    yield path
    core_db.get_db.cache_clear()


@pytest.fixture
def client(db_path):
    """A client whose writes land in the per-test database."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def raw_client(db_path):
    """A client that returns 500 responses instead of re-raising them.

    `TestClient` re-raises a server exception by default, which would make the
    "no stack trace reaches the client" test unable to see the response at all.
    """
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


VALID_INCIDENT = {
    "title": "Parcel missing from outbound pallet",
    "description": "Pallet 42 left the dock two units short; both are unscanned.",
    "category": "lost_parcel",
    "origin": "branch",
    "branch": "la_warehouse",
}
