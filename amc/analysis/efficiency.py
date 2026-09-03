from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .runs import LaneRun
from .selection import _norm_args

# Step counts, loops, redundant calls, retries, error rate - all per lane, all
# from observed events. A loop can hide inside a correct final answer, so it's
# flagged explicitly rather than left buried in a step total.


@dataclass(frozen=True)
class Loop:
    tool: str
    args: str
    length: int          # consecutive identical calls
    start_seq: int


@dataclass(frozen=True)
class ToolStats:
    tool: str
    calls: int
    errors: int
    retries: int         # a call to this tool immediately after it errored

    @property
    def error_rate(self) -> float:
        return self.errors / self.calls if self.calls else 0.0


@dataclass(frozen=True)
class Efficiency:
    lane_id: str
    steps: int                       # tool calls
    llm_turns: int                   # model calls
    distinct_tool_calls: int         # distinct (tool, normalised args)
    redundant_calls: int             # extra calls beyond the first for each (tool, args)
    loops: tuple[Loop, ...]
    per_tool: tuple[ToolStats, ...]

    @property
    def has_loop(self) -> bool:
        return bool(self.loops)


def _loops(tool_events) -> tuple[Loop, ...]:
    out: list[Loop] = []
    i = 0
    n = len(tool_events)
    while i < n:
        key = (tool_events[i].name, _norm_args(tool_events[i].args_json))
        j = i + 1
        while j < n and (tool_events[j].name, _norm_args(tool_events[j].args_json)) == key:
            j += 1
        if j - i >= 2:
            out.append(Loop(tool=key[0], args=key[1], length=j - i,
                            start_seq=tool_events[i].seq))
        i = j
    return tuple(out)


def _per_tool(tool_events) -> tuple[ToolStats, ...]:
    calls: Counter = Counter()
    errors: Counter = Counter()
    retries: Counter = Counter()

    last_errored_tool: str | None = None
    for e in tool_events:
        calls[e.name] += 1
        if last_errored_tool == e.name:
            retries[e.name] += 1
        if e.error_type is not None:
            errors[e.name] += 1
            last_errored_tool = e.name
        else:
            last_errored_tool = None

    return tuple(
        ToolStats(tool=t, calls=calls[t], errors=errors[t], retries=retries[t])
        for t in sorted(calls)
    )


def efficiency(lane: LaneRun) -> Efficiency:
    tools = lane.tool_events
    pairs = [(e.name, _norm_args(e.args_json)) for e in tools]
    counts = Counter(pairs)

    return Efficiency(
        lane_id=lane.lane_id,
        steps=len(tools),
        llm_turns=len(lane.llm_events),
        distinct_tool_calls=len(counts),
        redundant_calls=sum(c - 1 for c in counts.values() if c > 1),
        loops=_loops(tools),
        per_tool=_per_tool(tools),
    )
