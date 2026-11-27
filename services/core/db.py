"""The API's TinyDB handle (users, profiles, password resets, suppliers, incidents).

TinyDB is not thread-safe, and FastAPI runs every sync route and dependency
(`get_current_user` included) in a threadpool. Its `JSONStorage` keeps one open
file handle and reads by seeking to the end, back to the start, then
`json.load`; writes seek, write, then truncate. Two threads on that handle
interleave those steps: a reader parses half the file (`JSONDecodeError`, a 500
that reaches the browser as a CORS error), and two writers leave trailing bytes
that corrupt the file. A table's read-modify-write (`insert`, `update`, ...) is
also two storage calls, so concurrent writers lose each other's changes.

So the database here is `ThreadSafeTinyDB`: one re-entrant lock per database,
shared by its storage and its tables. Each storage read or write uses the file
handle alone, and each table write (read, modify, write) and each cached search
is one step. Operations are quick, so serializing them costs nothing visible.
The lock is per process: two processes writing the same file (the API and a
seed script) are still not coordinated, as before.
"""

import os
import threading
from collections.abc import Callable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from tinydb import TinyDB
from tinydb.storages import JSONStorage
from tinydb.table import Document, Table

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = ROOT_DIR / "data" / "suppliers.json"


class ThreadSafeJSONStorage(JSONStorage):
    """`JSONStorage` whose file handle is used by one thread at a time."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.lock = threading.RLock()

    def read(self) -> dict[str, dict[str, Any]] | None:
        with self.lock:
            return super().read()

    def write(self, data: dict[str, dict[str, Any]]) -> None:
        with self.lock:
            super().write(data)


class ThreadSafeTable(Table):
    """A table whose writes and cached searches are atomic under the storage's lock.

    `_update_table` is every write's read-modify-write; `search` reads and fills
    the query cache that writes clear, so a search racing a write could otherwise
    cache a stale result, or find its cache entry evicted mid-lookup.
    """

    @property
    def _lock(self) -> threading.RLock:
        storage = self.storage
        assert isinstance(storage, ThreadSafeJSONStorage)
        return storage.lock

    def _update_table(self, updater: Callable[[dict[int, Mapping]], None]) -> None:
        with self._lock:
            super()._update_table(updater)

    def search(self, cond: Any) -> list[Document]:
        with self._lock:
            return super().search(cond)

    def clear_cache(self) -> None:
        with self._lock:
            super().clear_cache()


class ThreadSafeTinyDB(TinyDB):
    table_class = ThreadSafeTable
    default_storage_class = ThreadSafeJSONStorage


def get_db_path() -> Path:
    return Path(os.getenv("DB_PATH", DEFAULT_DB_PATH))


@lru_cache(maxsize=1)
def get_db() -> TinyDB:
    path = get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return ThreadSafeTinyDB(path, indent=2, ensure_ascii=False)


def get_table(name: str) -> Table:
    """Return a TinyDB table. Each domain owns its own table name."""
    return get_db().table(name)
