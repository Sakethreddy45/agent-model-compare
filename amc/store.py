from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, Sequence

from .context import LaneStatus, Role
from .policy import Isolation

# The store is the seam between the recorder and whatever holds the data.
# SQLite for v1, Postgres later. The recorder depends only on the `Store`
# protocol below, never on this module's concrete class - swapping the backend
# must not touch recorder.py.


class EventKind(str, Enum):
    LLM = "llm"
    TOOL = "tool"


class LatencySource(str, Enum):
    MEASURED = "measured"    # timed directly on this call
    REPLAYED = "replayed"    # the primary's observed latency for the same call
    SAMPLED = "sampled"      # drawn from a fitted per-tool distribution
    MEDIAN = "median"        # per-tool median fallback, primary never made this call


class ResponseSource(str, Enum):
    REAL = "real"
    FIXTURE = "fixture"
    SCHEMA = "schema"
    TEMPLATE = "template"


# --- rows -------------------------------------------------------------------
#
# Frozen: a row read back from the store is a fact, not a mutable handle. The
# recorder edits its in-memory copy with dataclasses.replace and re-writes.
#
# Every count/latency field is `... | None`. None means "not reported"; it must
# survive a round trip distinct from 0 (invariant 8, and the phase 4 pass
# condition). No column carries a DEFAULT 0 and none but the structural keys is
# NOT NULL.


@dataclass(frozen=True)
class QueryRow:
    query_id: str
    input: str
    created_at: str
    was_sampled: bool
    primary_lane_id: str


@dataclass(frozen=True)
class LaneRow:
    lane_id: str
    query_id: str
    role: Role
    model_requested: str | None
    model_observed: str | None
    status: LaneStatus
    error_type: str | None
    started_at: str | None
    ended_at: str | None
    final_output: str | None
    contaminated_at_step: int | None


@dataclass(frozen=True)
class EventRow:
    event_id: str
    lane_id: str
    seq: int
    node_name: str | None
    kind: EventKind
    name: str
    args_json: str | None
    tokens_in: int | None
    tokens_out: int | None
    cached_tokens: int | None
    latency_ms: float | None
    latency_source: LatencySource | None
    response_source: ResponseSource | None
    isolation_mode: Isolation | None
    blocked: bool
    error_type: str | None


class Store(Protocol):
    """The small interface the recorder writes through. A Postgres
    implementation supplies the same five methods and nothing in recorder.py
    changes.

    `write` takes batches and must apply one batch atomically (all rows or
    none). `lanes` rows are upserts keyed on `lane_id` - a lane is written once
    when it starts and again when it finishes, possibly in the same batch.
    """

    def write(
        self,
        *,
        queries: Sequence[QueryRow] = (),
        lanes: Sequence[LaneRow] = (),
        events: Sequence[EventRow] = (),
    ) -> None: ...

    def read_query(self, query_id: str) -> QueryRow | None: ...

    def read_lanes(self, query_id: str) -> list[LaneRow]: ...

    def read_events(self, lane_id: str) -> list[EventRow]: ...

    def close(self) -> None: ...


_DDL = """
CREATE TABLE IF NOT EXISTS queries (
    query_id        TEXT PRIMARY KEY,
    input           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    was_sampled     INTEGER NOT NULL,
    primary_lane_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lanes (
    lane_id              TEXT PRIMARY KEY,
    query_id             TEXT NOT NULL,
    role                 TEXT NOT NULL,
    model_requested      TEXT,
    model_observed       TEXT,
    status               TEXT NOT NULL,
    error_type           TEXT,
    started_at           TEXT,
    ended_at             TEXT,
    final_output         TEXT,
    contaminated_at_step INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    lane_id         TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    node_name       TEXT,
    kind            TEXT NOT NULL,
    name            TEXT NOT NULL,
    args_json       TEXT,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    cached_tokens   INTEGER,
    latency_ms      REAL,
    latency_source  TEXT,
    response_source TEXT,
    isolation_mode  TEXT,
    blocked         INTEGER NOT NULL,
    error_type      TEXT
);
-- Judgements get their own table (data model, docs/architecture.md) so a judge
-- score can never sit in a row next to a measured number. No write path in v1.
CREATE TABLE IF NOT EXISTS judgments (
    judgment_id TEXT PRIMARY KEY,
    query_id    TEXT NOT NULL,
    lane_a      TEXT NOT NULL,
    lane_b      TEXT NOT NULL,
    verdict     TEXT,
    judge_model TEXT,
    run_index   INTEGER
);
CREATE INDEX IF NOT EXISTS ix_lanes_query ON lanes(query_id);
CREATE INDEX IF NOT EXISTS ix_events_lane ON events(lane_id, seq);
"""

_LANE_COLS = (
    "lane_id", "query_id", "role", "model_requested", "model_observed",
    "status", "error_type", "started_at", "ended_at", "final_output",
    "contaminated_at_step",
)
_EVENT_COLS = (
    "event_id", "lane_id", "seq", "node_name", "kind", "name", "args_json",
    "tokens_in", "tokens_out", "cached_tokens", "latency_ms", "latency_source",
    "response_source", "isolation_mode", "blocked", "error_type",
)


def _enum(value: str | None, cls):
    return cls(value) if value is not None else None


class SqliteStore:
    """`Store` over one SQLite connection.

    One connection, shared across the threadpool that `recorder` drains from
    (`check_same_thread=False`), with a lock so a drain is serialised against a
    concurrent read. Postgres would use a real pool here instead.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if self._path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_DDL)
        self._conn.commit()

    def write(
        self,
        *,
        queries: Sequence[QueryRow] = (),
        lanes: Sequence[LaneRow] = (),
        events: Sequence[EventRow] = (),
    ) -> None:
        with self._lock:
            try:
                cur = self._conn.cursor()
                cur.executemany(
                    "INSERT OR IGNORE INTO queries "
                    "(query_id, input, created_at, was_sampled, primary_lane_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        (q.query_id, q.input, q.created_at, int(q.was_sampled),
                         q.primary_lane_id)
                        for q in queries
                    ],
                )
                cur.executemany(
                    f"INSERT INTO lanes ({', '.join(_LANE_COLS)}) "
                    f"VALUES ({', '.join('?' * len(_LANE_COLS))}) "
                    "ON CONFLICT(lane_id) DO UPDATE SET "
                    + ", ".join(f"{c}=excluded.{c}" for c in _LANE_COLS[1:]),
                    [self._lane_params(row) for row in lanes],
                )
                cur.executemany(
                    f"INSERT OR REPLACE INTO events ({', '.join(_EVENT_COLS)}) "
                    f"VALUES ({', '.join('?' * len(_EVENT_COLS))})",
                    [self._event_params(row) for row in events],
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    @staticmethod
    def _lane_params(r: LaneRow) -> tuple:
        return (
            r.lane_id, r.query_id, r.role.value, r.model_requested,
            r.model_observed, r.status.value, r.error_type, r.started_at,
            r.ended_at, r.final_output, r.contaminated_at_step,
        )

    @staticmethod
    def _event_params(r: EventRow) -> tuple:
        return (
            r.event_id, r.lane_id, r.seq, r.node_name, r.kind.value, r.name,
            r.args_json, r.tokens_in, r.tokens_out, r.cached_tokens,
            r.latency_ms,
            r.latency_source.value if r.latency_source is not None else None,
            r.response_source.value if r.response_source is not None else None,
            r.isolation_mode.value if r.isolation_mode is not None else None,
            int(r.blocked), r.error_type,
        )

    def read_query(self, query_id: str) -> QueryRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM queries WHERE query_id = ?", (query_id,)
            ).fetchone()
        if row is None:
            return None
        return QueryRow(
            query_id=row["query_id"],
            input=row["input"],
            created_at=row["created_at"],
            was_sampled=bool(row["was_sampled"]),
            primary_lane_id=row["primary_lane_id"],
        )

    def read_lanes(self, query_id: str) -> list[LaneRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM lanes WHERE query_id = ? ORDER BY started_at, lane_id",
                (query_id,),
            ).fetchall()
        return [
            LaneRow(
                lane_id=r["lane_id"],
                query_id=r["query_id"],
                role=Role(r["role"]),
                model_requested=r["model_requested"],
                model_observed=r["model_observed"],
                status=LaneStatus(r["status"]),
                error_type=r["error_type"],
                started_at=r["started_at"],
                ended_at=r["ended_at"],
                final_output=r["final_output"],
                contaminated_at_step=r["contaminated_at_step"],
            )
            for r in rows
        ]

    def read_events(self, lane_id: str) -> list[EventRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE lane_id = ? ORDER BY seq", (lane_id,)
            ).fetchall()
        return [
            EventRow(
                event_id=r["event_id"],
                lane_id=r["lane_id"],
                seq=r["seq"],
                node_name=r["node_name"],
                kind=EventKind(r["kind"]),
                name=r["name"],
                args_json=r["args_json"],
                tokens_in=r["tokens_in"],
                tokens_out=r["tokens_out"],
                cached_tokens=r["cached_tokens"],
                latency_ms=r["latency_ms"],
                latency_source=_enum(r["latency_source"], LatencySource),
                response_source=_enum(r["response_source"], ResponseSource),
                isolation_mode=_enum(r["isolation_mode"], Isolation),
                blocked=bool(r["blocked"]),
                error_type=r["error_type"],
            )
            for r in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
