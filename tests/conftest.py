"""Test fixtures for the incident manager.

Every test runs against its own TinyDB file in a temporary directory. The
handle is cached with `lru_cache`, so redirecting `DB_PATH` is not enough on its
own - the cache has to be cleared as well, before and after, or one test's
incidents leak into the next and into `data/suppliers.json`.
"""

import pytest
from fastapi.testclient import TestClient

from services.core import db as core_db
from services.main import app


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
