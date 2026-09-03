from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from enum import Enum

from .runs import LaneRun

# THE FRAMING RULE (docs/metrics.md): reference-based trajectory comparison is
# inherently limited - multiple valid paths usually exist, and the primary's is
# not ground truth. So everything here is DESCRIPTIVE:
#
#   "lane B called `fetch` where lane A called `search` at the planning node"
#
# never a score like "lane B: 36%". Jaccard overlap and argument match rate are
# reported as the set-similarity / agreement fractions they are, not as a
# ranking of which model did better.


def _norm_args(args_json: str | None) -> str:
    """Re-canonicalise so key order / whitespace / float spelling can't read as
    a mismatch."""
    if not args_json:
        return ""
    try:
        return json.dumps(json.loads(args_json), sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return args_json


def _tool_multiset(lane: LaneRun) -> Counter:
    return Counter(e.name for e in lane.tool_events)


# --- tool set overlap --------------------------------------------------


@dataclass(frozen=True)
class ToolOverlap:
    lane_a: str
    lane_b: str
    multiset_a: dict[str, int]
    multiset_b: dict[str, int]
    multiset_jaccard: float      # |A ∩ B| / |A ∪ B| counting multiplicity
    set_jaccard: float           # same, ignoring how many times each tool was called
    only_a: tuple[str, ...]      # tools lane A used and lane B never did
    only_b: tuple[str, ...]


def tool_set_overlap(a: LaneRun, b: LaneRun) -> ToolOverlap:
    ma, mb = _tool_multiset(a), _tool_multiset(b)

    inter = sum((ma & mb).values())
    union = sum((ma | mb).values())
    set_inter = len(set(ma) & set(mb))
    set_union = len(set(ma) | set(mb))

    return ToolOverlap(
        lane_a=a.lane_id,
        lane_b=b.lane_id,
        multiset_a=dict(ma),
        multiset_b=dict(mb),
        multiset_jaccard=inter / union if union else 1.0,
        set_jaccard=set_inter / set_union if set_union else 1.0,
        only_a=tuple(sorted(set(ma) - set(mb))),
        only_b=tuple(sorted(set(mb) - set(ma))),
    )


# --- node-keyed alignment -------------------------------------------
#
# Compare like-named nodes, not sequence positions: under parallel fan-out
# arrival order varies for reasons unrelated to the model, so position 3 vs
# position 3 reports scheduling noise as divergence. We bucket each lane's tool
# calls by node_name and compare the k-th visit to each node. When no event
# carries a node_name this degenerates to positional comparison, and the result
# says so.


@dataclass(frozen=True)
class _Step:
    node: str | None
    occurrence: int
    a: object | None      # EventRow or None
    b: object | None


def _aligned_steps(a: LaneRun, b: LaneRun) -> tuple[list[_Step], str]:
    a_tools, b_tools = a.tool_events, b.tool_events
    has_nodes = any(e.node_name for e in a_tools) or any(e.node_name for e in b_tools)

    if not has_nodes:
        steps = []
        for i in range(max(len(a_tools), len(b_tools))):
            steps.append(_Step(
                node=None, occurrence=i,
                a=a_tools[i] if i < len(a_tools) else None,
                b=b_tools[i] if i < len(b_tools) else None,
            ))
        return steps, "positional"

    def by_node(events):
        out: dict[str | None, list] = {}
        for e in events:
            out.setdefault(e.node_name, []).append(e)
        return out

    a_by, b_by = by_node(a_tools), by_node(b_tools)

    order: list[str | None] = []
    for e in list(a_tools) + list(b_tools):
        if e.node_name not in order:
            order.append(e.node_name)

    steps = []
    for node in order:
        a_list, b_list = a_by.get(node, []), b_by.get(node, [])
        for j in range(max(len(a_list), len(b_list))):
            steps.append(_Step(
                node=node, occurrence=j,
                a=a_list[j] if j < len(a_list) else None,
                b=b_list[j] if j < len(b_list) else None,
            ))
    return steps, "node"


# --- divergence point ---------------------------------------------


@dataclass(frozen=True)
class DivergencePoint:
    diverged: bool
    alignment: str                 # "node" | "positional"
    step: int | None               # 1-based ordinal among compared steps
    node: str | None
    primary_tool: str | None       # None => the primary had no call at this step
    shadow_tool: str | None
    within_first_two_steps: bool


def describe_divergence(primary: LaneRun, shadow: LaneRun) -> DivergencePoint:
    """The first compared step where the two lanes chose differently. ~60% of
    divergence lands in the first two steps, so `within_first_two_steps` is
    surfaced for cheap early-inconsistency signal."""
    steps, alignment = _aligned_steps(primary, shadow)

    for idx, s in enumerate(steps, start=1):
        pa = s.a.name if s.a is not None else None
        sb = s.b.name if s.b is not None else None
        if pa != sb:
            return DivergencePoint(
                diverged=True,
                alignment=alignment,
                step=idx,
                node=s.node,
                primary_tool=pa,
                shadow_tool=sb,
                within_first_two_steps=idx <= 2,
            )

    return DivergencePoint(
        diverged=False, alignment=alignment, step=None, node=None,
        primary_tool=None, shadow_tool=None, within_first_two_steps=False,
    )


def divergence_sentence(dp: DivergencePoint, primary_label: str, shadow_label: str) -> str:
    if not dp.diverged:
        return (f"{shadow_label} visited the same node trajectory as "
                f"{primary_label} ({dp.alignment} alignment).")
    if dp.node is not None:
        where = f"node {dp.node!r} (step {dp.step}, {dp.alignment} alignment)"
    else:
        where = f"step {dp.step} ({dp.alignment} alignment)"
    chose_p = f"called {dp.primary_tool!r}" if dp.primary_tool else "made no call"
    chose_s = f"called {dp.shadow_tool!r}" if dp.shadow_tool else "made no call"
    return (f"{shadow_label} first diverged from {primary_label} at {where}: "
            f"{primary_label} {chose_p}, {shadow_label} {chose_s}.")


# --- argument match rate -----------------------------------------


@dataclass(frozen=True)
class ArgMismatch:
    step: int
    node: str | None
    tool: str
    primary_args: str
    shadow_args: str


@dataclass(frozen=True)
class ArgMatch:
    comparable: int          # steps where both lanes called the same tool
    matched: int
    match_rate: float | None  # None when nothing was comparable
    mismatches: tuple[ArgMismatch, ...]


def argument_match(primary: LaneRun, shadow: LaneRun) -> ArgMatch:
    """Of the steps where both lanes called the *same* tool, how often did the
    normalised arguments agree. Free-text output rarely matches even between
    consistent runs, so this compares argument patterns only."""
    steps, _ = _aligned_steps(primary, shadow)
    comparable = matched = 0
    mismatches: list[ArgMismatch] = []

    for idx, s in enumerate(steps, start=1):
        if s.a is None or s.b is None or s.a.name != s.b.name:
            continue
        comparable += 1
        pa, sb = _norm_args(s.a.args_json), _norm_args(s.b.args_json)
        if pa == sb:
            matched += 1
        else:
            mismatches.append(ArgMismatch(idx, s.node, s.a.name, pa, sb))

    return ArgMatch(
        comparable=comparable,
        matched=matched,
        match_rate=(matched / comparable) if comparable else None,
        mismatches=tuple(mismatches),
    )


# --- matching modes -------------------------------------------


class MatchMode(str, Enum):
    STRICT = "strict"        # identical tool sequence, in order
    UNORDERED = "unordered"  # same multiset of tool calls
    SUBSET = "subset"        # a's calls are all present in b (with multiplicity)
    SUPERSET = "superset"    # b's calls are all present in a
    NODE_EDGES = "node_edges"  # same set of (from_node -> to_node) transitions


def _node_edges(lane: LaneRun) -> set[tuple[str | None, str | None]]:
    nodes = [e.node_name for e in lane.tool_events]
    return set(zip(nodes, nodes[1:]))


def trajectory_match(a: LaneRun, b: LaneRun, mode: MatchMode) -> bool:
    seq_a = [e.name for e in a.tool_events]
    seq_b = [e.name for e in b.tool_events]

    if mode is MatchMode.STRICT:
        return seq_a == seq_b
    if mode is MatchMode.UNORDERED:
        return Counter(seq_a) == Counter(seq_b)
    if mode is MatchMode.SUBSET:
        return not (Counter(seq_a) - Counter(seq_b))
    if mode is MatchMode.SUPERSET:
        return not (Counter(seq_b) - Counter(seq_a))
    if mode is MatchMode.NODE_EDGES:
        return _node_edges(a) == _node_edges(b)
    raise ValueError(f"unknown match mode {mode!r}")
