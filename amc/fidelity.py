from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .provenance import EventKind, LatencySource, ResponseSource
from .store import EventRow, LaneRow

# The honest alternative to pretending substitution didn't happen: per lane,
# how much of what we're about to compare was executed for real versus
# replayed, synthesised, or made up - and whether the lane drifted far enough
# to be contaminated. A lane below a fidelity threshold is flagged and kept
# out of headline comparisons.
#
# Pure functions over stored EventRows. No network, no LLM.

_SUBSTITUTED_LATENCY = {
    LatencySource.REPLAYED, LatencySource.SAMPLED, LatencySource.MEDIAN,
}


@dataclass(frozen=True)
class FidelitySummary:
    lane_label: str
    tool_calls: int
    executed_real: int
    from_fixture: int
    schema_synthesised: int
    from_template: int
    stubbed: int
    blocked: int
    latency_measured_ms: float
    latency_substituted_ms: float
    contaminated_at_step: int | None

    @property
    def latency_total_ms(self) -> float:
        return self.latency_measured_ms + self.latency_substituted_ms

    @property
    def real_fraction(self) -> float:
        return self.executed_real / self.tool_calls if self.tool_calls else 1.0

    @property
    def latency_substituted_fraction(self) -> float:
        total = self.latency_total_ms
        return self.latency_substituted_ms / total if total else 0.0

    def is_low_fidelity(self, threshold: float = 0.5) -> bool:
        return self.contaminated_at_step is not None or self.real_fraction < threshold


def summarise(lane: LaneRow, events: Sequence[EventRow]) -> FidelitySummary:
    tools = [e for e in events if e.kind is EventKind.TOOL]
    by_source = {s: 0 for s in ResponseSource}
    blocked = 0
    lat_measured = 0.0
    lat_substituted = 0.0

    for e in tools:
        if e.blocked:
            blocked += 1
        elif e.response_source is not None:
            by_source[e.response_source] += 1
        if e.latency_ms is None:
            continue
        if e.latency_source is LatencySource.MEASURED:
            lat_measured += e.latency_ms
        elif e.latency_source in _SUBSTITUTED_LATENCY:
            lat_substituted += e.latency_ms

    label = lane.model_observed or lane.model_requested or lane.role.value
    return FidelitySummary(
        lane_label=f"{lane.lane_id} ({label})",
        tool_calls=len(tools),
        executed_real=by_source[ResponseSource.REAL],
        from_fixture=by_source[ResponseSource.FIXTURE],
        schema_synthesised=by_source[ResponseSource.SCHEMA],
        from_template=by_source[ResponseSource.TEMPLATE],
        stubbed=by_source[ResponseSource.STUB],
        blocked=blocked,
        latency_measured_ms=lat_measured,
        latency_substituted_ms=lat_substituted,
        contaminated_at_step=lane.contaminated_at_step,
    )


def _pct(n: int, total: int) -> str:
    return f"{round(100 * n / total):>3d}%" if total else "  -"


def render(s: FidelitySummary) -> str:
    n = s.tool_calls
    lat_total = s.latency_total_ms
    lines = [
        f"lane: {s.lane_label}",
        f"  tool calls:          {n:>5d}",
        f"  executed for real:   {s.executed_real:>5d}  ({_pct(s.executed_real, n)})",
        f"  served from fixture: {s.from_fixture:>5d}  ({_pct(s.from_fixture, n)})",
        f"  schema-synthesised:  {s.schema_synthesised:>5d}  ({_pct(s.schema_synthesised, n)})",
    ]
    if s.from_template:
        lines.append(f"  from template:       {s.from_template:>5d}  ({_pct(s.from_template, n)})")
    if s.stubbed:
        lines.append(f"  stubbed (no source): {s.stubbed:>5d}  ({_pct(s.stubbed, n)})")
    if s.blocked:
        lines.append(f"  hard-blocked:        {s.blocked:>5d}  ({_pct(s.blocked, n)})")
    lines.append(
        f"  latency substituted: {s.latency_substituted_ms:.0f}ms of "
        f"{lat_total:.0f}ms ({_pct(round(s.latency_substituted_ms), round(lat_total))})"
    )
    step = s.contaminated_at_step
    lines.append(f"  contaminated:        {'at step ' + str(step) if step is not None else 'no'}")
    if s.is_low_fidelity():
        lines.append("  -> LOW FIDELITY: excluded from headline comparisons")
    return "\n".join(lines)
