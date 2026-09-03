"""Deterministic metrics over stored events.

Pure functions - no network, no LLM, no I/O beyond `load_query` reading a
`Store`. Divergence is reported descriptively, never as a score (see
`selection`). Cost is computed at read time from tokens plus a price version,
never stored (see `cost`).
"""
from ..fidelity import FidelitySummary, render as render_fidelity, summarise
from .cost import (
    BUNDLED_PRICES,
    CostBreakdown,
    ModelPrice,
    PriceTable,
    TokenTotals,
    cost_for_lane,
    token_totals,
)
from .efficiency import Efficiency, Loop, ToolStats, efficiency
from .latency import LatencyProfile, latency_profile, percentile
from .runs import LaneRun, QueryRun, load_query
from .selection import (
    ArgMatch,
    ArgMismatch,
    DivergencePoint,
    MatchMode,
    ToolOverlap,
    argument_match,
    describe_divergence,
    divergence_sentence,
    tool_set_overlap,
    trajectory_match,
)


def fidelity_of(lane: LaneRun) -> FidelitySummary:
    """The fidelity meta-metrics for a lane (share executed for real vs
    virtualised, share of latency substituted, contamination)."""
    return summarise(lane.lane, lane.events)


__all__ = [
    "load_query",
    "LaneRun",
    "QueryRun",
    "tool_set_overlap",
    "ToolOverlap",
    "describe_divergence",
    "divergence_sentence",
    "DivergencePoint",
    "argument_match",
    "ArgMatch",
    "ArgMismatch",
    "trajectory_match",
    "MatchMode",
    "efficiency",
    "Efficiency",
    "Loop",
    "ToolStats",
    "token_totals",
    "TokenTotals",
    "cost_for_lane",
    "CostBreakdown",
    "PriceTable",
    "ModelPrice",
    "BUNDLED_PRICES",
    "latency_profile",
    "LatencyProfile",
    "percentile",
    "fidelity_of",
    "FidelitySummary",
    "render_fidelity",
]
