"""The API's TinyDB handle under concurrency (`services/core/db.py`).

FastAPI runs sync routes and dependencies in a threadpool, so several threads
share the one TinyDB handle. Without the lock, concurrent reads parsed half a
file (`JSONDecodeError` → a 500 in `get_current_user`, seen in the browser as a
CORS error), concurrent writes lost inserts and left the JSON file corrupt
("Extra data"). Each test fails on the unlocked handle and passes on the locked one.
"""

import json
import threading
from collections import Counter

from tinydb import Query

from services.core import db as core_db
from services.core import security
from tests.test_agent_api import sign_in

THREADS = 8
ROUNDS = 40


def run_together(worker, count: int = THREADS) -> Counter[str]:
    """Start `count` threads at the same moment; the failures they raised, by type."""
    failures: Counter[str] = Counter()
    start = threading.Barrier(count)

    def guarded(n: int) -> None:
        start.wait()
        try:
            worker(n)
        except Exception as error:  # noqa: BLE001 - counted, then asserted on
            failures[type(error).__name__] += 1

    threads = [threading.Thread(target=guarded, args=(n,)) for n in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return failures


def test_the_database_is_the_thread_safe_one(db_path):
    database = core_db.get_db()

    assert isinstance(database, core_db.ThreadSafeTinyDB)
    assert isinstance(core_db.get_table("users"), core_db.ThreadSafeTable)


def test_concurrent_reads_and_writes_neither_fail_nor_lose_data(db_path):
    core_db.get_table("users").insert({"id": "known", "email": "a@example.com"})

    def worker(n: int) -> None:
        table = core_db.get_table("users")
        for i in range(ROUNDS):
            table.insert({"id": f"w{n}-{i}"})
            # What get_current_user does on every request.
            assert table.get(Query().id == "known") is not None
            assert table.search(Query().id == f"w{n}-{i}")

    failures = run_together(worker)

    assert failures == Counter()
    assert len(core_db.get_table("users")) == 1 + THREADS * ROUNDS
    # The file itself is whole: one JSON document, every insert in it.
    stored = json.loads(db_path.read_text(encoding="utf-8"))
    assert len(stored["users"]) == 1 + THREADS * ROUNDS


def test_concurrent_authenticated_requests_never_fail(client, monkeypatch):
    """The production symptom: every request authenticates against TinyDB in a worker thread."""
    monkeypatch.setattr(security, "BCRYPT_ROUNDS", 4)
    headers, _ = sign_in(client, "sales@trackflow.com", role="manager")
    statuses: Counter[int] = Counter()
    lock = threading.Lock()

    def worker(n: int) -> None:
        for _ in range(10):
            response = client.get("/auth/me", headers=headers)
            with lock:
                statuses[response.status_code] += 1

    failures = run_together(worker)

    assert failures == Counter()
    assert statuses == Counter({200: THREADS * 10})
