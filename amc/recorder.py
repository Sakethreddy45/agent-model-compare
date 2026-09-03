from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .context import Lane, LaneStatus, Role
from .interceptor import ToolEvent, set_sink
from .store import (
    EventKind,
    EventRow,
    LaneRow,
    LatencySource,
    QueryRow,
    ResponseSource,
    Store,
)

# One event per llm/tool call, one lane row per lane, one query row per query -
# buffered in memory and flushed to the `Store` off the primary's path. The
# record_* methods only append to lists/dicts (invariant 1: logging adds no
# latency to the primary); the flush loop does the SQLite work inside
# asyncio.to_thread so it never blocks the event loop either.


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Recorder:
    """Collects a run and writes it through a `Store`.

    Lifecycle: `async with Recorder(store) as rec:` (or `await rec.start()` /
    `await rec.aclose()`). While started it owns the process-wide interceptor
    tool-event sink, so use one recorder at a time.
    """

    def __init__(
        self,
        store: Store,
        *,
        flush_interval: float = 0.25,
        attach_interceptor_sink: bool = True,
    ) -> None:
        self._store = store
        self._flush_interval = flush_interval
        self._attach_sink = attach_interceptor_sink

        self._queries: list[QueryRow] = []
        self._lanes: dict[str, LaneRow] = {}
        self._dirty: set[str] = set()
        self._events: list[EventRow] = []
        self._seq: dict[str, int] = {}

        self._latest_query_id: str | None = None
        self._flush_task: asyncio.Task | None = None
        self._closed = False
        self._warned_orphan = False

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> "Recorder":
        if self._attach_sink:
            set_sink(self.record_tool_event)
        self._ensure_flush_loop()
        return self

    async def __aenter__(self) -> "Recorder":
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def flush(self) -> None:
        """Drain every buffered row to the store now. Await this before
        reading the store back."""
        await self._drain()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._attach_sink:
            set_sink(None)
        if self._flush_task is not None:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None
        await self._drain()
        await asyncio.to_thread(self._store.close)

    def _ensure_flush_loop(self) -> None:
        if self._flush_task is not None or self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop yet; flush() / aclose() still work synchronously
        self._flush_task = loop.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            await self._drain()

    async def _drain(self) -> None:
        queries, self._queries = self._queries, []
        dirty, self._dirty = self._dirty, set()
        events, self._events = self._events, []
        lanes = [self._lanes[i] for i in dirty if i in self._lanes]
        if not (queries or lanes or events):
            return
        await asyncio.to_thread(
            self._store.write, queries=queries, lanes=lanes, events=events
        )

    # --- recording ------------------------------------------------------

    def start_query(
        self,
        query_id: str,
        *,
        input: str,
        was_sampled: bool,
        primary_lane_id: str,
    ) -> None:
        self._latest_query_id = query_id
        self._queries.append(QueryRow(
            query_id=query_id,
            input=input,
            created_at=_now(),
            was_sampled=was_sampled,
            primary_lane_id=primary_lane_id,
        ))
        self._ensure_flush_loop()

    def start_lane(self, lane: Lane, *, query_id: str) -> None:
        if lane.id in self._lanes:
            return  # idempotent; keep the original started_at
        self._lanes[lane.id] = LaneRow(
            lane_id=lane.id,
            query_id=query_id,
            role=lane.role,
            model_requested=lane.model,
            model_observed=None,
            status=LaneStatus.RUNNING,
            error_type=None,
            started_at=_now(),
            ended_at=None,
            final_output=None,
            contaminated_at_step=None,
        )
        self._dirty.add(lane.id)
        self._ensure_flush_loop()

    def finish_lane(
        self,
        lane: Lane,
        *,
        status: LaneStatus,
        final_output: str | None = None,
        error_type: str | None = None,
        contaminated_at_step: int | None = None,
    ) -> None:
        row = self._lanes.get(lane.id)
        if row is None:
            self._orphan_lane(lane.id, lane.role)
            row = self._lanes[lane.id]
        self._lanes[lane.id] = replace(
            row,
            status=status,
            ended_at=_now(),
            final_output=final_output if final_output is not None else row.final_output,
            error_type=error_type if error_type is not None else row.error_type,
            contaminated_at_step=(
                contaminated_at_step if contaminated_at_step is not None
                else row.contaminated_at_step
            ),
        )
        self._dirty.add(lane.id)
        self._ensure_flush_loop()

    def mark_collision(self, lane_id: str) -> None:
        """Invariant 6: this lane's observed model matched the primary's."""
        row = self._lanes.get(lane_id)
        if row is None:
            return
        self._lanes[lane_id] = replace(row, status=LaneStatus.INVALID)
        self._dirty.add(lane_id)

    def record_tool_event(self, ev: ToolEvent) -> None:
        if ev.lane_id is None:
            return  # no lane in scope - unattributable; interceptor logged it
        if ev.lane_id not in self._lanes:
            self._orphan_lane(ev.lane_id, _role_from_str(ev.role))
        self._events.append(EventRow(
            event_id=uuid4().hex,
            lane_id=ev.lane_id,
            seq=self._next_seq(ev.lane_id),
            node_name=None,
            kind=EventKind.TOOL,
            name=ev.name,
            args_json=ev.args_json,
            tokens_in=None,
            tokens_out=None,
            cached_tokens=None,
            latency_ms=ev.latency_ms,
            latency_source=(
                LatencySource.MEASURED if ev.latency_ms is not None else None
            ),
            response_source=ResponseSource.REAL if ev.executed else None,
            isolation_mode=ev.isolation,
            blocked=not ev.executed,
            error_type=ev.error_type,
        ))
        self._ensure_flush_loop()

    def record_model_call(
        self,
        lane: Lane,
        adapter: Any,
        response: Any,
        *,
        node_name: str | None = None,
        latency_ms: float | None = None,
    ) -> None:
        """Record one real model call: token usage from the provider's usage
        object, plus the model the provider says it actually used. Not an LLM
        call itself - pure extraction (invariant 7). This is what gives the
        runner each lane's `model_observed` for the invariant 6 check."""
        usage = adapter.extract_usage(response)
        observed = adapter.observed_model(response)

        row = self._lanes.get(lane.id)
        if row is None:
            self.start_lane(lane, query_id=self._latest_query_id or "")
            row = self._lanes[lane.id]
        if observed is not None and row.model_observed != observed:
            self._lanes[lane.id] = replace(row, model_observed=observed)
            self._dirty.add(lane.id)

        self._events.append(EventRow(
            event_id=uuid4().hex,
            lane_id=lane.id,
            seq=self._next_seq(lane.id),
            node_name=node_name,
            kind=EventKind.LLM,
            name=lane.model or observed or "",
            args_json=None,
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            cached_tokens=usage.cached_tokens,
            latency_ms=latency_ms,
            latency_source=(
                LatencySource.MEASURED if latency_ms is not None else None
            ),
            response_source=ResponseSource.REAL,  # every model call is real
            isolation_mode=None,
            blocked=False,
            error_type=None,
        ))
        self._ensure_flush_loop()

    # --- reads used by the runner -------------------------------------

    def model_observed(self, lane_id: str) -> str | None:
        row = self._lanes.get(lane_id)
        return row.model_observed if row is not None else None

    # --- internals --------------------------------------------------

    def _next_seq(self, lane_id: str) -> int:
        n = self._seq.get(lane_id, 0) + 1
        self._seq[lane_id] = n
        return n

    def _orphan_lane(self, lane_id: str, role: Role) -> None:
        if not self._warned_orphan:
            print(f"[amc] recorder: event for lane {lane_id!r} with no "
                  "start_lane; attributing to the latest query")
            self._warned_orphan = True
        self._lanes[lane_id] = LaneRow(
            lane_id=lane_id,
            query_id=self._latest_query_id or "",
            role=role,
            model_requested=None,
            model_observed=None,
            status=LaneStatus.RUNNING,
            error_type=None,
            started_at=_now(),
            ended_at=None,
            final_output=None,
            contaminated_at_step=None,
        )
        self._dirty.add(lane_id)


def _role_from_str(role: str) -> Role:
    try:
        return Role(role)
    except ValueError:
        return Role.SHADOW
