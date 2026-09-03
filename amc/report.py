from __future__ import annotations

from dataclasses import dataclass

from .analysis import (
    BUNDLED_PRICES,
    ArgMatch,
    CostBreakdown,
    DivergencePoint,
    Efficiency,
    LatencyProfile,
    LaneRun,
    MatchMode,
    PriceTable,
    QueryRun,
    TokenTotals,
    ToolOverlap,
    argument_match,
    cost_for_lane,
    describe_divergence,
    divergence_sentence,
    efficiency,
    fidelity_of,
    latency_profile,
    token_totals,
    tool_set_overlap,
    trajectory_match,
)
from .context import LaneStatus, Role
from .fidelity import FidelitySummary
from .fidelity import render as render_fidelity

# The top of the dependency chain: analysis -> report. Nothing imports this.
#
# The report is three sections, in this order and no other:
#
#   1. Measured facts   - tokens, cost, latency, step counts. All algorithmic,
#      all from observed events. Cost is recomputed here at read time from a
#      price version; no dollar figure was ever stored.
#   2. Fidelity summary - what each lane executed for real versus replayed or
#      synthesised, and whether it was contaminated. A lane below the fidelity
#      threshold is shown but excluded from the headline comparison.
#   3. Divergence       - DESCRIPTIVE. Multiple valid paths usually exist and
#      the primary's trajectory is not ground truth, so a shadow that diverges
#      took a different valid route, not a wrong one. Rendered as observations
#      ("chose `fetch` where the primary chose `search` at node act2"), never
#      as a score or a ranking.
#
# There is no judgement section. The LLM judge layer is deferred past v1 and
# lives in its own table so a judged number can never sit beside a measured one.


@dataclass(frozen=True)
class LaneReport:
    lane_id: str
    role: Role
    model: str | None
    status: LaneStatus
    error_type: str | None
    tokens: TokenTotals
    cost: CostBreakdown
    latency: LatencyProfile
    efficiency: Efficiency
    fidelity: FidelitySummary
    excluded_from_headline: bool   # fidelity below threshold or contaminated


@dataclass(frozen=True)
class DivergenceReport:
    """One shadow lane compared against the primary. Every field is an
    observation; none is a score."""
    shadow_id: str
    shadow_model: str | None
    primary_id: str
    primary_model: str | None
    overlap: ToolOverlap
    point: DivergencePoint
    sentence: str
    arg_match: ArgMatch
    strict_match: bool
    unordered_match: bool
    node_edge_match: bool


@dataclass(frozen=True)
class Report:
    query_id: str
    query_input: str
    created_at: str
    was_sampled: bool
    price_version: str
    fidelity_threshold: float
    primary: LaneReport | None
    shadows: tuple[LaneReport, ...]
    divergences: tuple[DivergenceReport, ...]

    @property
    def lanes(self) -> tuple[LaneReport, ...]:
        return ((self.primary,) if self.primary is not None else ()) + self.shadows

    @property
    def excluded_lanes(self) -> tuple[str, ...]:
        return tuple(l.lane_id for l in self.lanes if l.excluded_from_headline)


def build_report(
    run: QueryRun,
    *,
    price_table: PriceTable = BUNDLED_PRICES,
    fidelity_threshold: float = 0.5,
) -> Report:
    """Structured report over a loaded query run. Pure: no I/O, no LLM."""

    def lane_report(lr: LaneRun) -> LaneReport:
        fs = fidelity_of(lr)
        return LaneReport(
            lane_id=lr.lane_id,
            role=lr.lane.role,
            model=lr.model,
            status=lr.lane.status,
            error_type=lr.lane.error_type,
            tokens=token_totals(lr),
            cost=cost_for_lane(lr, price_table),
            latency=latency_profile(lr),
            efficiency=efficiency(lr),
            fidelity=fs,
            excluded_from_headline=fs.is_low_fidelity(fidelity_threshold),
        )

    primary = lane_report(run.primary) if run.primary is not None else None
    shadows = tuple(lane_report(s) for s in run.shadows)

    divergences: list[DivergenceReport] = []
    if run.primary is not None:
        p = run.primary
        for s in run.shadows:
            dp = describe_divergence(p, s)
            divergences.append(
                DivergenceReport(
                    shadow_id=s.lane_id,
                    shadow_model=s.model,
                    primary_id=p.lane_id,
                    primary_model=p.model,
                    overlap=tool_set_overlap(p, s),
                    point=dp,
                    sentence=divergence_sentence(dp, p.lane_id, s.lane_id),
                    arg_match=argument_match(p, s),
                    strict_match=trajectory_match(p, s, MatchMode.STRICT),
                    unordered_match=trajectory_match(p, s, MatchMode.UNORDERED),
                    node_edge_match=trajectory_match(p, s, MatchMode.NODE_EDGES),
                )
            )

    return Report(
        query_id=run.query.query_id,
        query_input=run.query.input,
        created_at=run.query.created_at,
        was_sampled=run.query.was_sampled,
        price_version=price_table.version,
        fidelity_threshold=fidelity_threshold,
        primary=primary,
        shadows=shadows,
        divergences=tuple(divergences),
    )


# --- rendering ------------------------------------------------------------

_W = 68


def _trim(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _ct(n: int | None) -> str:
    """Null is not zero: an unreported count renders as a dash, 0 renders as 0."""
    return "—" if n is None else f"{n:,}"


def _ms(x: float | None) -> str:
    return "—" if x is None else f"{x:,.0f}"


def _money(x: float | None) -> str:
    return "—" if x is None else f"${x:,.4f}"


def _delta_pct(new: float, base: float) -> str:
    if base == 0:
        return "n/a"
    d = (new - base) / base * 100
    if -0.5 < d < 0.5:
        return "±0%"
    return f"{d:+.0f}%"


def _lane_roster(r: Report) -> list[str]:
    out = ["LANES"]
    for l in r.lanes:
        flag = "  [LOW FIDELITY]" if l.excluded_from_headline else ""
        err = f" ({l.error_type})" if l.error_type else ""
        out.append(
            f"  {l.role.value:<8}{l.lane_id:<16}{(l.model or '?'):<20}"
            f"{l.status.value}{err}{flag}"
        )
    if r.excluded_lanes:
        out.append("")
        out.append(
            f"  {', '.join(r.excluded_lanes)}: below the "
            f"{r.fidelity_threshold:.0%} real-execution threshold (or "
            f"contaminated)."
        )
        out.append("  Shown for completeness, kept out of the headline comparison.")
    return out


def _cost_block(r: Report) -> list[str]:
    out = [
        f"Cost   price version {r.price_version}   (tokens exact; cost computed "
        f"at read time)",
        f"  {'lane':<16}{'model':<20}{'in':>10}{'cached':>9}{'out':>9}{'cost':>13}",
    ]
    for l in r.lanes:
        t, c = l.tokens, l.cost
        cost = _money(c.total_cost)
        if c.total_cost is not None and c.incomplete:
            cost += "*"
        out.append(
            f"  {l.lane_id:<16}{(l.model or '?'):<20}"
            f"{_ct(t.tokens_in):>10}{_ct(t.cached_tokens):>9}"
            f"{_ct(t.tokens_out):>9}{cost:>13}"
        )
    if any(not l.cost.priced for l in r.lanes):
        out.append("  —  no price-table entry for that model; not estimated.")
    if any(l.cost.total_cost is not None and l.cost.incomplete for l in r.lanes):
        out.append("  *  partial: a model call reported no token count "
                   "(lower bound).")
    missing = [
        l.lane_id for l in r.lanes
        if l.tokens.missing_in or l.tokens.missing_out
    ]
    if missing:
        out.append(f"  token counts missing on some calls: {', '.join(missing)}")
    return out


def _latency_block(r: Report) -> list[str]:
    out = [
        "Latency   ms, nearest-rank p50 / p95.  Measured and substituted are",
        "          reported separately and never blended into one figure.",
        f"  {'lane':<16}{'obs':>5}{'p50':>7}{'p95':>7}"
        f"{'measured':>11}{'+subst':>9}{'=total':>9}",
    ]
    for l in r.lanes:
        lp = l.latency
        out.append(
            f"  {l.lane_id:<16}{lp.measured_count:>5}"
            f"{_ms(lp.measured_p50):>7}{_ms(lp.measured_p95):>7}"
            f"{_ms(lp.measured_total_ms):>11}{_ms(lp.substituted_total_ms):>9}"
            f"{_ms(lp.combined_total_ms):>9}"
        )
    out.append("  'measured' = real model + passthrough calls; '+subst' = "
               "replayed tool latency.")
    return out


def _efficiency_block(r: Report) -> list[str]:
    out = [
        "Efficiency",
        f"  {'lane':<16}{'steps':>6}{'llm turns':>11}{'distinct':>10}"
        f"{'redundant':>11}{'loops':>7}",
    ]
    for l in r.lanes:
        e = l.efficiency
        out.append(
            f"  {l.lane_id:<16}{e.steps:>6}{e.llm_turns:>11}"
            f"{e.distinct_tool_calls:>10}{e.redundant_calls:>11}{len(e.loops):>7}"
        )
    loop_lines = []
    for l in r.lanes:
        for lp in l.efficiency.loops:
            loop_lines.append(
                f"    {l.lane_id}: {lp.tool} ×{lp.length} from seq {lp.start_seq}"
            )
    if loop_lines:
        out.append("  repeated identical calls:")
        out += loop_lines
    err_lines = []
    for l in r.lanes:
        for ts in l.efficiency.per_tool:
            if ts.errors or ts.retries:
                err_lines.append(
                    f"    {l.lane_id}: {ts.tool} — {ts.calls} calls, "
                    f"{ts.errors} error(s) ({ts.error_rate:.0%}), "
                    f"{ts.retries} retry(ies)"
                )
    if err_lines:
        out.append("  retries / errors:")
        out += err_lines
    return out


def _vs_primary_block(r: Report) -> list[str]:
    if r.primary is None or not r.shadows:
        return []
    p = r.primary
    out = ["vs primary   (measured differences, not a quality ranking)"]
    for s in r.shadows:
        parts: list[str] = []
        if p.cost.total_cost and s.cost.total_cost is not None:
            parts.append(f"cost {_delta_pct(s.cost.total_cost, p.cost.total_cost)}")
        if p.latency.combined_total_ms and s.latency.combined_total_ms:
            parts.append(
                "latency "
                f"{_delta_pct(s.latency.combined_total_ms, p.latency.combined_total_ms)}"
            )
        parts.append(f"steps {s.efficiency.steps - p.efficiency.steps:+d}")
        parts.append(f"llm turns {s.efficiency.llm_turns - p.efficiency.llm_turns:+d}")
        tag = "  [low fidelity]" if s.excluded_from_headline else ""
        out.append(f"  {s.lane_id}: {' · '.join(parts)}{tag}")
    return out


def _divergence_block(d: DivergenceReport) -> list[str]:
    ov = d.overlap
    out = [
        f"{d.shadow_id} ({d.shadow_model or '?'})  vs  "
        f"{d.primary_id} ({d.primary_model or '?'})",
        f"  {d.sentence}",
        f"  tool-set overlap:   multiset Jaccard {ov.multiset_jaccard:.2f} · "
        f"set Jaccard {ov.set_jaccard:.2f}",
    ]
    if ov.only_a:
        out.append(f"  only {d.primary_id} used:  {', '.join(ov.only_a)}")
    if ov.only_b:
        out.append(f"  only {d.shadow_id} used:  {', '.join(ov.only_b)}")
    am = d.arg_match
    if am.match_rate is None:
        out.append("  argument agreement:  no comparable same-tool calls")
    else:
        out.append(
            f"  argument agreement:  {am.matched}/{am.comparable} "
            f"comparable same-tool calls matched"
        )
        for m in am.mismatches:
            where = f"node {m.node}" if m.node else f"step {m.step}"
            out.append(f"    {where} {m.tool}: {m.primary_args} vs {m.shadow_args}")
    out.append(
        f"  trajectory match:    strict {_yn(d.strict_match)} · "
        f"unordered {_yn(d.unordered_match)} · node-edges {_yn(d.node_edge_match)}"
    )
    return out


def _yn(b: bool) -> str:
    return "yes" if b else "no"


def render_report(
    run: QueryRun,
    *,
    price_table: PriceTable = BUNDLED_PRICES,
    fidelity_threshold: float = 0.5,
) -> str:
    """The full text report. Builds the structured `Report` and renders it."""
    r = build_report(run, price_table=price_table, fidelity_threshold=fidelity_threshold)
    bar = "=" * _W
    rule = "-" * _W
    sampled = "sampled" if r.was_sampled else "not sampled"

    out: list[str] = [
        bar,
        " SHADOW COMPARISON REPORT",
        f" query {r.query_id}  ·  \"{_trim(r.query_input, 46)}\"",
        f" recorded {r.created_at}  ·  {sampled}",
        bar,
        "",
    ]
    out += _lane_roster(r)
    out += ["", rule,
            "1 · MEASURED FACTS   algorithmic, computed from observed events",
            rule, ""]
    out += _cost_block(r)
    out += [""]
    out += _latency_block(r)
    out += [""]
    out += _efficiency_block(r)
    vs = _vs_primary_block(r)
    if vs:
        out += [""] + vs

    out += ["", rule,
            "2 · FIDELITY SUMMARY   what was substituted, and how much",
            rule, ""]
    for l in r.lanes:
        out.append(render_fidelity(l.fidelity))
        out.append("")

    out += [rule, "3 · DIVERGENCE   descriptive", rule,
            "  Multiple valid paths usually exist; the primary's trajectory is",
            "  not ground truth. A shadow that diverges took a different valid",
            "  route, not a wrong one. Reported as observations, never scored.",
            ""]
    if r.primary is None:
        out.append("  No primary lane recorded — divergence needs a baseline.")
    elif not r.divergences:
        out.append("  No shadow lanes to compare.")
    else:
        for i, d in enumerate(r.divergences):
            if i:
                out.append("")
            out += _divergence_block(d)

    return "\n".join(out).rstrip() + "\n"


__all__ = [
    "Report",
    "LaneReport",
    "DivergenceReport",
    "build_report",
    "render_report",
]
