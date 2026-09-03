from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from ..provenance import EventKind, LatencySource
from .runs import LaneRun

# p50 and p95, never a mean alone, and never a single blended figure: measured
# latency and substituted (replayed / sampled / median) latency are reported
# separately, per docs/virtual-tool-layer.md. Latency is attributed per step so
# a 20-step loop shows up instead of being buried in a trace total.

_SUBSTITUTED = {LatencySource.REPLAYED, LatencySource.SAMPLED, LatencySource.MEDIAN}


def percentile(values: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile: rank = ceil(p/100 * N), value = sorted[rank-1].
    No interpolation, so hand-checking a fixture is unambiguous."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    if p <= 0:
        return xs[0]
    rank = math.ceil(p / 100 * len(xs))
    return xs[min(rank, len(xs)) - 1]


@dataclass(frozen=True)
class LatencyProfile:
    lane_id: str

    measured_count: int
    measured_p50: float | None
    measured_p95: float | None
    measured_total_ms: float

    substituted_count: int
    substituted_total_ms: float

    combined_p50: float | None       # over measured + substituted together
    combined_p95: float | None
    combined_total_ms: float

    tool_total_ms: float
    llm_total_ms: float
    per_node_ms: tuple[tuple[str, float], ...]   # node_name (or tool name) -> summed ms
    per_step_ms: tuple[float | None, ...]        # tool calls in seq order


def latency_profile(lane: LaneRun) -> LatencyProfile:
    measured: list[float] = []
    substituted: list[float] = []
    per_node: dict[str, float] = {}
    tool_total = llm_total = 0.0

    for e in lane.events:
        if e.latency_ms is None:
            continue
        if e.latency_source is LatencySource.MEASURED:
            measured.append(e.latency_ms)
        elif e.latency_source in _SUBSTITUTED:
            substituted.append(e.latency_ms)
        else:
            continue  # a latency with no provenance is not counted either way

        if e.kind is EventKind.TOOL:
            tool_total += e.latency_ms
        else:
            llm_total += e.latency_ms
        key = e.node_name or e.name
        per_node[key] = per_node.get(key, 0.0) + e.latency_ms

    combined = measured + substituted
    return LatencyProfile(
        lane_id=lane.lane_id,
        measured_count=len(measured),
        measured_p50=percentile(measured, 50),
        measured_p95=percentile(measured, 95),
        measured_total_ms=sum(measured),
        substituted_count=len(substituted),
        substituted_total_ms=sum(substituted),
        combined_p50=percentile(combined, 50),
        combined_p95=percentile(combined, 95),
        combined_total_ms=sum(combined),
        tool_total_ms=tool_total,
        llm_total_ms=llm_total,
        per_node_ms=tuple(sorted(per_node.items())),
        per_step_ms=tuple(e.latency_ms for e in lane.tool_events),
    )
