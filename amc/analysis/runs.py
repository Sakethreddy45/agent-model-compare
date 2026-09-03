from __future__ import annotations

from dataclasses import dataclass

from ..context import Role
from ..provenance import EventKind
from ..store import EventRow, LaneRow, QueryRow, Store

# The in-memory shape every analysis function works over. `load_query` is the
# only thing here that touches a Store; everything downstream is a pure
# function of these frozen rows - no network, no LLM, no I/O.


@dataclass(frozen=True)
class LaneRun:
    lane: LaneRow
    events: tuple[EventRow, ...]

    @property
    def lane_id(self) -> str:
        return self.lane.lane_id

    @property
    def model(self) -> str | None:
        return self.lane.model_observed or self.lane.model_requested

    @property
    def tool_events(self) -> tuple[EventRow, ...]:
        return tuple(e for e in self.events if e.kind is EventKind.TOOL)

    @property
    def llm_events(self) -> tuple[EventRow, ...]:
        return tuple(e for e in self.events if e.kind is EventKind.LLM)


@dataclass(frozen=True)
class QueryRun:
    query: QueryRow
    lanes: tuple[LaneRun, ...]

    @property
    def primary(self) -> LaneRun | None:
        return next((l for l in self.lanes if l.lane.role is Role.PRIMARY), None)

    @property
    def shadows(self) -> tuple[LaneRun, ...]:
        return tuple(l for l in self.lanes if l.lane.role is Role.SHADOW)

    def lane(self, lane_id: str) -> LaneRun | None:
        return next((l for l in self.lanes if l.lane_id == lane_id), None)


def load_query(store: Store, query_id: str) -> QueryRun:
    query = store.read_query(query_id)
    if query is None:
        raise KeyError(f"no query {query_id!r} in store")

    lanes = []
    for lane in store.read_lanes(query_id):
        events = tuple(sorted(store.read_events(lane.lane_id), key=lambda e: e.seq))
        lanes.append(LaneRun(lane=lane, events=events))

    # primary first, then shadows in a stable order
    lanes.sort(key=lambda l: (l.lane.role is not Role.PRIMARY, l.lane_id))
    return QueryRun(query=query, lanes=tuple(lanes))
