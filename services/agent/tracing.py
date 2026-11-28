"""The run trace: which nodes ran, in what order, what each read and produced, and why.

Every run of the graph leaves one `agent_runs` row and one `agent_run_events`
row per step: a `node` row for each node that ran (its declared inputs, its
state update or its error, its timing) and an `edge` row for each routing
decision (where it went and the reason). Rows are numbered by `seq` in the order
they happened; a resumed run continues the numbering with the next `attempt`.

The tables live in the agent store file next to LangGraph's checkpoints
(`AGENT_STORE_PATH`), so a run stays queryable after it ends:

    sqlite3 data/agent/agent_runs.sqlite \
      "SELECT seq, kind, name, status FROM agent_run_events WHERE run_id = '<id>' ORDER BY seq"

Each write opens its own short connection, which keeps the store safe to use
from FastAPI's worker threads.
"""

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal

from services.agent.config import get_agent_settings
from services.agent.state import SOURCE_OF_NODE

RunStatus = Literal["running", "completed", "failed"]
EventKind = Literal["node", "edge"]
EventStatus = Literal["ok", "error"]

RUNS_TABLE: Final = """
CREATE TABLE IF NOT EXISTS {name} (
    run_id          TEXT PRIMARY KEY,
    user_id         TEXT,
    graph_version   TEXT NOT NULL,
    question        TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    outcome         TEXT CHECK (outcome IN
                        ('answered', 'no_information', 'rejected', 'memory_reply', 'refused')),
    answer          TEXT,
    attempts        INTEGER NOT NULL DEFAULT 1,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    error_node      TEXT,
    error_type      TEXT,
    error_message   TEXT,
    conversation_id TEXT
);
"""

RUNS_INDEXES: Final = """
CREATE INDEX IF NOT EXISTS agent_runs_started_at_idx ON agent_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS agent_runs_user_id_idx ON agent_runs (user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS agent_runs_conversation_idx ON agent_runs (conversation_id, started_at);
"""

# Every column of `agent_runs`, in the order of RUNS_TABLE.
_RUN_COLUMNS: Final = (
    "run_id",
    "user_id",
    "graph_version",
    "question",
    "status",
    "outcome",
    "answer",
    "attempts",
    "started_at",
    "finished_at",
    "error_node",
    "error_type",
    "error_message",
    "conversation_id",
)

# One row per guardrail event (SEC-114): what blocked, refused, redirected or
# cleaned something in a run, and why. No message text: the rule id says why.
GUARDRAIL_EVENTS_TABLE: Final = """
CREATE TABLE IF NOT EXISTS guardrail_events (
    run_id          TEXT NOT NULL REFERENCES agent_runs (run_id),
    seq             INTEGER NOT NULL,
    recorded_at     TEXT NOT NULL,
    conversation_id TEXT,
    user_id         TEXT,
    audience        TEXT,
    guardrail       TEXT NOT NULL,
    action          TEXT NOT NULL,
    failure_type    TEXT NOT NULL CHECK (failure_type IN ('structural', 'content', 'security')),
    rule            TEXT NOT NULL,
    source          TEXT,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS guardrail_events_recorded_at_idx
    ON guardrail_events (recorded_at);
"""

SCHEMA: Final = (
    RUNS_TABLE.format(name="agent_runs")
    + """
CREATE TABLE IF NOT EXISTS agent_run_events (
    run_id        TEXT NOT NULL REFERENCES agent_runs (run_id),
    seq           INTEGER NOT NULL,
    attempt       INTEGER NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('node', 'edge')),
    name          TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('ok', 'error')),
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    duration_ms   REAL,
    input_json    TEXT,
    output_json   TEXT,
    error_type    TEXT,
    error_message TEXT,
    PRIMARY KEY (run_id, seq)
);
"""
    + GUARDRAIL_EVENTS_TABLE
)


def _migrate_runs_table(connection: sqlite3.Connection) -> None:
    """Bring an older `agent_runs` table up to date, keeping every row.

    Before MEM-092 it had no `conversation_id` and no `memory_reply` outcome;
    before SEC-114, no `refused` outcome. SQLite cannot change a CHECK
    constraint in place, so the table is rebuilt: created under a new name,
    filled with the columns it had, swapped. `agent_run_events` and
    `guardrail_events` refer to it by name, so their rows stay attached.
    """
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'agent_runs'"
    ).fetchone()
    if row is None or "'refused'" in row[0]:
        return
    present = {info[1] for info in connection.execute("PRAGMA table_info(agent_runs)")}
    columns = ", ".join(column for column in _RUN_COLUMNS if column in present)
    connection.executescript(
        "DROP TABLE IF EXISTS agent_runs_migrating;"
        + RUNS_TABLE.format(name="agent_runs_migrating")
        + f"INSERT INTO agent_runs_migrating ({columns}) SELECT {columns} FROM agent_runs;"
        "DROP TABLE agent_runs;"
        "ALTER TABLE agent_runs_migrating RENAME TO agent_runs;"
    )


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def utc_iso(value: str | datetime) -> str:
    """A time as the store writes it (UTC, milliseconds); a naive time is read as UTC."""
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="milliseconds")


def _dump(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


def _load(text: str | None) -> Any:
    return None if text is None else json.loads(text)


@dataclass(frozen=True)
class TraceEvent:
    seq: int
    attempt: int
    kind: EventKind
    name: str
    status: EventStatus
    started_at: str
    finished_at: str | None
    duration_ms: float | None
    input: Any
    output: Any
    error_type: str | None
    error_message: str | None


@dataclass(frozen=True)
class RunTrace:
    run_id: str
    user_id: str | None
    graph_version: str
    question: str
    status: RunStatus
    outcome: str | None
    answer: str | None
    attempts: int
    started_at: str
    finished_at: str | None
    error_node: str | None
    error_type: str | None
    error_message: str | None
    events: tuple[TraceEvent, ...] = ()
    # The conversation the run belongs to (MEM-092); None for runs recorded before it.
    conversation_id: str | None = None

    def node_names(self) -> list[str]:
        """The nodes that ran, in order (a failed node included)."""
        return [event.name for event in self.events if event.kind == "node"]

    def node_events(self, name: str) -> list[TraceEvent]:
        return [event for event in self.events if event.kind == "node" and event.name == name]

    def edge_events(self) -> list[TraceEvent]:
        return [event for event in self.events if event.kind == "edge"]

    def sources(self) -> list[str]:
        """The sources the run consulted, in the order it consulted them.

        `incident_manager`, `inventory_manager` and/or `knowledge_base`, read
        from the node rows: a source counts once its node ran, whether it
        succeeded or not (the node row says which).
        """
        consulted: list[str] = []
        for name in self.node_names():
            source = SOURCE_OF_NODE.get(name)
            if source is not None and source not in consulted:
                consulted.append(source)
        return consulted


@dataclass(frozen=True)
class GuardrailEvent:
    run_id: str
    seq: int
    recorded_at: str
    conversation_id: str | None
    user_id: str | None
    audience: str | None
    guardrail: str
    action: str
    failure_type: str
    rule: str
    source: str | None


@dataclass(frozen=True)
class GuardrailCount:
    guardrail: str
    action: str
    failure_type: str
    count: int
    # In how many runs.
    runs: int


@dataclass(frozen=True)
class GuardrailSummary:
    """How many times each guardrail was triggered in a window (and a conversation)."""

    since: str | None
    until: str | None
    conversation_id: str | None
    runs: int
    counts: tuple[GuardrailCount, ...]

    @property
    def total(self) -> int:
        return sum(count.count for count in self.counts)

    def by(self, field: str) -> dict[str, int]:
        """Totals per `guardrail`, `action` or `failure_type`."""
        totals: dict[str, int] = {}
        for count in self.counts:
            key = getattr(count, field)
            totals[key] = totals.get(key, 0) + count.count
        return totals

    def as_counter(self) -> dict[tuple[str, str, str], int]:
        return {(c.guardrail, c.action, c.failure_type): c.count for c in self.counts}


def _run_from_row(row: sqlite3.Row, events: tuple[TraceEvent, ...] = ()) -> RunTrace:
    return RunTrace(
        run_id=row["run_id"],
        user_id=row["user_id"],
        graph_version=row["graph_version"],
        question=row["question"],
        status=row["status"],
        outcome=row["outcome"],
        answer=row["answer"],
        attempts=row["attempts"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error_node=row["error_node"],
        error_type=row["error_type"],
        error_message=row["error_message"],
        events=events,
        conversation_id=row["conversation_id"],
    )


def _event_from_row(row: sqlite3.Row) -> TraceEvent:
    return TraceEvent(
        seq=row["seq"],
        attempt=row["attempt"],
        kind=row["kind"],
        name=row["name"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        duration_ms=row["duration_ms"],
        input=_load(row["input_json"]),
        output=_load(row["output_json"]),
        error_type=row["error_type"],
        error_message=row["error_message"],
    )


class TraceStore:
    """Reads and writes run traces in one SQLite file; creates its tables on first use."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
        # executescript() commits as it goes, so the migration runs on its own.
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            _migrate_runs_table(connection)
            connection.executescript(RUNS_INDEXES)
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # --- Writing -------------------------------------------------------------------

    def start_run(
        self,
        run_id: str,
        *,
        question: str,
        user_id: str | None,
        graph_version: str,
        conversation_id: str | None = None,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO agent_runs (run_id, user_id, graph_version, question, status, "
                "started_at, conversation_id) VALUES (?, ?, ?, ?, 'running', ?, ?)",
                (run_id, user_id, graph_version, question, utc_now(), conversation_id),
            )

    def start_attempt(self, run_id: str) -> int:
        """Mark a failed run as running again; returns the new attempt number."""
        with self._transaction() as connection:
            connection.execute(
                "UPDATE agent_runs SET attempts = attempts + 1, status = 'running', "
                "finished_at = NULL, error_node = NULL, error_type = NULL, error_message = NULL "
                "WHERE run_id = ?",
                (run_id,),
            )
            row = connection.execute(
                "SELECT attempts FROM agent_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row["attempts"])

    def add_event(
        self,
        run_id: str,
        *,
        attempt: int,
        kind: EventKind,
        name: str,
        status: EventStatus,
        started_at: str,
        finished_at: str | None = None,
        duration_ms: float | None = None,
        input: Any = None,
        output: Any = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> int:
        """Append one event; returns its `seq`."""
        with self._transaction() as connection:
            seq = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM agent_run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO agent_run_events (run_id, seq, attempt, kind, name, status, "
                "started_at, finished_at, duration_ms, input_json, output_json, error_type, "
                "error_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    seq,
                    attempt,
                    kind,
                    name,
                    status,
                    started_at,
                    finished_at,
                    duration_ms,
                    _dump(input),
                    _dump(output),
                    error_type,
                    error_message,
                ),
            )
        return int(seq)

    def finish_run(
        self,
        run_id: str,
        *,
        status: RunStatus,
        outcome: str | None = None,
        answer: str | None = None,
        error_node: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE agent_runs SET status = ?, outcome = ?, answer = ?, finished_at = ?, "
                "error_node = ?, error_type = ?, error_message = ? WHERE run_id = ?",
                (
                    status,
                    outcome,
                    answer,
                    utc_now(),
                    error_node,
                    error_type,
                    error_message,
                    run_id,
                ),
            )

    # --- Reading -------------------------------------------------------------------

    def get_trace(self, run_id: str, *, owner: str | None = None) -> RunTrace | None:
        """The run and its events in order, or None. With `owner`, only that user's run."""
        with self._transaction() as connection:
            run = connection.execute(
                "SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None or (owner is not None and run["user_id"] != owner):
                return None
            rows = connection.execute(
                "SELECT * FROM agent_run_events WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
        return _run_from_row(run, tuple(_event_from_row(row) for row in rows))

    def list_runs(self, *, limit: int = 20, user_id: str | None = None) -> list[RunTrace]:
        """The most recent runs, newest first, without their events."""
        query = "SELECT * FROM agent_runs"
        params: tuple[Any, ...] = ()
        if user_id is not None:
            query += " WHERE user_id = ?"
            params = (user_id,)
        query += " ORDER BY started_at DESC, rowid DESC LIMIT ?"
        with self._transaction() as connection:
            rows = connection.execute(query, (*params, limit)).fetchall()
        return [_run_from_row(row) for row in rows]

    # --- Guardrail events (SEC-114) ------------------------------------------------

    def add_guardrail_events(
        self,
        run_id: str,
        events: list[Mapping[str, Any]],
        *,
        conversation_id: str | None,
        user_id: str | None,
        audience: str | None,
    ) -> int:
        """Record a run's events, numbered in order; returns how many were new.

        A resumed run hands over all its events again, earlier ones included:
        those already recorded (same run and position) are skipped.
        """
        stamp = utc_now()
        with self._transaction() as connection:
            before = connection.total_changes
            connection.executemany(
                "INSERT OR IGNORE INTO guardrail_events (run_id, seq, recorded_at, "
                "conversation_id, user_id, audience, guardrail, action, failure_type, rule, "
                "source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        seq,
                        stamp,
                        conversation_id,
                        user_id,
                        audience,
                        str(event["guardrail"]),
                        str(event["action"]),
                        str(event["failure_type"]),
                        str(event["rule"]),
                        event.get("source"),
                    )
                    for seq, event in enumerate(events, 1)
                ],
            )
            added = connection.total_changes - before
        return int(added)

    def guardrail_events(
        self, *, run_id: str | None = None, since: str | None = None, limit: int = 100
    ) -> list[GuardrailEvent]:
        """Recorded events, newest first: one run's, or every run's since a time."""
        query = "SELECT * FROM guardrail_events"
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if since is not None:
            clauses.append("recorded_at >= ?")
            params.append(utc_iso(since))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY recorded_at DESC, run_id, seq DESC LIMIT ?"
        with self._transaction() as connection:
            rows = connection.execute(query, (*params, limit)).fetchall()
        return [
            GuardrailEvent(**{f.name: row[f.name] for f in fields(GuardrailEvent)}) for row in rows
        ]

    def guardrail_summary(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        conversation_id: str | None = None,
    ) -> GuardrailSummary:
        """How many times each guardrail was triggered, by action and failure type."""
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            since = utc_iso(since)
            clauses.append("recorded_at >= ?")
            params.append(since)
        if until is not None:
            until = utc_iso(until)
            clauses.append("recorded_at < ?")
            params.append(until)
        if conversation_id is not None:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT guardrail, action, failure_type, COUNT(*) AS n, COUNT(DISTINCT run_id) "
                f"AS runs FROM guardrail_events{where} GROUP BY guardrail, action, failure_type "
                "ORDER BY guardrail, action, failure_type",
                params,
            ).fetchall()
            runs = connection.execute(
                f"SELECT COUNT(DISTINCT run_id) FROM guardrail_events{where}", params
            ).fetchone()[0]
        return GuardrailSummary(
            since=since,
            until=until,
            conversation_id=conversation_id,
            runs=int(runs),
            counts=tuple(
                GuardrailCount(
                    guardrail=row["guardrail"],
                    action=row["action"],
                    failure_type=row["failure_type"],
                    count=int(row["n"]),
                    runs=int(row["runs"]),
                )
                for row in rows
            ),
        )


@lru_cache(maxsize=1)
def get_trace_store() -> TraceStore:
    return TraceStore(get_agent_settings().agent_store_path)
