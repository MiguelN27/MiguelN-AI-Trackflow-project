"""Where the approval graph's state is kept between a pause and its resume: a checkpointer.

Decided with the developer (2026-11-23): PostgreSQL, the same database as the
tickets, through LangGraph's `PostgresSaver`. A ticket waiting for its owners
can wait for days. Its paused graph must survive an API restart and a new
container, and be backed up with the ticket it belongs to. The checkpoint
tables live in their own schema (`RFP_CHECKPOINT_SCHEMA`, default
`rfp_graph`), with row level security on and no policies, like every RFP
table.

SQLite engines (the offline tests and the smoke run's temporary database) use
LangGraph's `SqliteSaver`, in a file next to the database.

One thread per ticket: `rfp-<ticket_id>` (`thread_id`). The departments are
branches of that thread; LangGraph gives each branch its own checkpoint
namespace, so two tickets, or two departments, never share a checkpoint.
"""

import contextlib
import sqlite3
import threading
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from sqlalchemy.engine import Engine

THREAD_PREFIX: Final = "rfp-"
# LangGraph's own checkpoint tables (`PostgresSaver.setup`).
CHECKPOINT_TABLES: Final = (
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
)

_lock = threading.Lock()
_savers: dict[int, tuple[Engine, BaseCheckpointSaver[Any], Any]] = {}


def thread_id(ticket_id: UUID | str) -> str:
    """The checkpoint thread of one ticket's approval run."""
    return f"{THREAD_PREFIX}{ticket_id}"


def _sqlite_path(engine: Engine) -> str:
    database = engine.url.database
    if not database or database == ":memory:":
        return ":memory:"
    path = Path(database)
    return str(path.with_name(f"{path.stem}-checkpoints.sqlite"))


def _sqlite_saver(engine: Engine) -> tuple[BaseCheckpointSaver[Any], Any]:
    # SqliteSaver serialises access with its own lock, so one connection is shared
    # by FastAPI's worker threads (as the knowledge agent's checkpointer).
    connection = sqlite3.connect(_sqlite_path(engine), check_same_thread=False)
    saver = SqliteSaver(connection)
    saver.setup()
    return saver, connection


def _postgres_saver(engine: Engine, schema: str) -> tuple[BaseCheckpointSaver[Any], Any]:
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg import sql
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    conninfo = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(conninfo, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
    pool: ConnectionPool[Any] = ConnectionPool(
        conninfo,
        min_size=1,
        max_size=4,
        open=True,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            # The Supabase pooler cannot keep prepared statements across transactions.
            "prepare_threshold": None,
            "options": f"-c search_path={schema}",
        },
    )
    saver = PostgresSaver(pool)  # type: ignore[arg-type]
    saver.setup()
    with pool.connection() as connection:
        for table in CHECKPOINT_TABLES:
            connection.execute(
                sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(
                    sql.Identifier(schema, table)
                )
            )
    return saver, pool


def get_checkpointer(engine: Engine, schema: str = "rfp_graph") -> BaseCheckpointSaver[Any]:
    """The checkpointer for this engine's database (one per engine, created once)."""
    with _lock:
        cached = _savers.get(id(engine))
        if cached is not None and cached[0] is engine:
            return cached[1]
        if engine.dialect.name == "postgresql":
            saver, resource = _postgres_saver(engine, schema)
        else:
            saver, resource = _sqlite_saver(engine)
        _savers[id(engine)] = (engine, saver, resource)
        return saver


def close_checkpointers() -> None:
    """Close every connection the checkpointers hold (API shutdown, tests)."""
    with _lock:
        entries = list(_savers.values())
        _savers.clear()
    for _, _, resource in entries:
        with contextlib.suppress(Exception):  # closing is best effort
            resource.close()
