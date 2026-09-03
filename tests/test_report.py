import json

import pytest

from amc.analysis import load_query
from amc.context import LaneStatus, Role
from amc.provenance import EventKind, LatencySource, ResponseSource
from amc.report import build_report, render_report
from amc.store import EventRow, LaneRow, QueryRow, SqliteStore


def _args(**kwargs) -> str:
    return json.dumps({"args": [], "kwargs": kwargs}, sort_keys=True)


def _ev(lane_id, seq, kind, name, node, *, args=None, tin=None, tout=None,
        cached=None, lat=None, lsrc=None, rsrc=None, err=None, blocked=False):
    return EventRow(
        event_id=f"{lane_id}-{seq}", lane_id=lane_id, seq=seq, node_name=node,
        kind=kind, name=name, args_json=args,
        tokens_in=tin, tokens_out=tout, cached_tokens=cached,
        latency_ms=lat, latency_source=lsrc, response_source=rsrc,
        isolation_mode=None, blocked=blocked, error_type=err,
    )


def _lane(lane_id, role, *, query_id="q1", requested=None, observed=None,
          status=LaneStatus.OK, contaminated=None):
    return LaneRow(
        lane_id=lane_id, query_id=query_id, role=role,
        model_requested=requested, model_observed=observed,
        status=status, error_type=None,
        started_at="t0", ended_at="t1", final_output="ok",
        contaminated_at_step=contaminated,
    )


@pytest.fixture
def run():
    """Primary + two shadows, hand-built so every figure below is checkable:

      q1-p  (opus-5)   search@act1, fetch@act2 ×2   - a loop, all real
      q1-s0 (sonnet-5) search@act1, summarize@act2, search@act3
                       - diverges at act2, served from fixture/schema -> LOW FIDELITY
      q1-s1 (haiku)    same trajectory as primary, all real
    """
    store = SqliteStore(":memory:")
    store.write(
        queries=[QueryRow("q1", "how many signups today", "t0", True, "q1-p")],
        lanes=[
            _lane("q1-p", Role.PRIMARY, observed="claude-opus-5"),
            _lane("q1-s0", Role.SHADOW,
                  requested="claude-sonnet-5", observed="claude-sonnet-5"),
            _lane("q1-s1", Role.SHADOW,
                  requested="claude-haiku-4-5", observed="claude-haiku-4-5"),
        ],
        events=[
            _ev("q1-p", 1, EventKind.LLM, "claude-opus-5", "plan",
                tin=1000, tout=200, cached=200),
            _ev("q1-p", 2, EventKind.TOOL, "search", "act1", args=_args(q="x"),
                lat=40.0, lsrc=LatencySource.MEASURED, rsrc=ResponseSource.REAL),
            _ev("q1-p", 3, EventKind.LLM, "claude-opus-5", "plan",
                tin=1500, tout=100, cached=0),
            _ev("q1-p", 4, EventKind.TOOL, "fetch", "act2", args=_args(id=1),
                lat=30.0, lsrc=LatencySource.MEASURED, rsrc=ResponseSource.REAL),
            _ev("q1-p", 5, EventKind.TOOL, "fetch", "act2", args=_args(id=1),
                lat=30.0, lsrc=LatencySource.MEASURED, rsrc=ResponseSource.REAL),

            _ev("q1-s0", 1, EventKind.LLM, "claude-sonnet-5", "plan",
                tin=1000, tout=250, cached=0),
            _ev("q1-s0", 2, EventKind.TOOL, "search", "act1", args=_args(q="x"),
                lat=40.0, lsrc=LatencySource.REPLAYED, rsrc=ResponseSource.FIXTURE),
            _ev("q1-s0", 3, EventKind.LLM, "claude-sonnet-5", "plan",
                tin=1600, tout=120, cached=0),
            _ev("q1-s0", 4, EventKind.TOOL, "summarize", "act2", args=_args(id=1),
                lat=None, lsrc=None, rsrc=ResponseSource.SCHEMA),
            _ev("q1-s0", 5, EventKind.TOOL, "search", "act3", args=_args(q="y"),
                lat=40.0, lsrc=LatencySource.REPLAYED, rsrc=ResponseSource.FIXTURE),

            _ev("q1-s1", 1, EventKind.LLM, "claude-haiku-4-5", "plan",
                tin=1000, tout=180, cached=0),
            _ev("q1-s1", 2, EventKind.TOOL, "search", "act1", args=_args(q="x"),
                lat=50.0, lsrc=LatencySource.MEASURED, rsrc=ResponseSource.REAL),
            _ev("q1-s1", 3, EventKind.LLM, "claude-haiku-4-5", "plan",
                tin=1400, tout=90, cached=0),
            _ev("q1-s1", 4, EventKind.TOOL, "fetch", "act2", args=_args(id=1),
                lat=25.0, lsrc=LatencySource.MEASURED, rsrc=ResponseSource.REAL),
            _ev("q1-s1", 5, EventKind.TOOL, "fetch", "act2", args=_args(id=1),
                lat=25.0, lsrc=LatencySource.MEASURED, rsrc=ResponseSource.REAL),
        ],
    )
    yield load_query(store, "q1")
    store.close()


# --- structure -------------------------------------------------------------


def test_build_report_has_primary_and_two_shadows(run):
    r = build_report(run)
    assert r.query_id == "q1"
    assert r.query_input == "how many signups today"
    assert r.primary.lane_id == "q1-p"
    assert [s.lane_id for s in r.shadows] == ["q1-s0", "q1-s1"]
    assert r.price_version == "amc-bundled-2026-02-01"


def test_measured_facts_are_carried_through(run):
    r = build_report(run)
    p = r.primary
    assert (p.tokens.tokens_in, p.tokens.tokens_out, p.tokens.cached_tokens) == (2500, 300, 200)
    assert p.cost.total_cost == pytest.approx(0.0573)          # opus-5 bundled rates
    assert p.latency.measured_p50 == 30.0 and p.latency.measured_p95 == 40.0
    assert p.latency.combined_total_ms == 100.0
    assert p.efficiency.steps == 3 and p.efficiency.redundant_calls == 1
    assert len(p.efficiency.loops) == 1


def test_low_fidelity_shadow_is_flagged_and_excluded(run):
    r = build_report(run)
    s0 = next(s for s in r.shadows if s.lane_id == "q1-s0")
    s1 = next(s for s in r.shadows if s.lane_id == "q1-s1")
    assert s0.excluded_from_headline is True       # 0/3 tool calls executed for real
    assert s1.excluded_from_headline is False
    assert r.excluded_lanes == ("q1-s0",)


def test_fidelity_threshold_is_configurable(run):
    # every lane's real_fraction is <= 1.0; demand more and all are excluded
    r = build_report(run, fidelity_threshold=1.01)
    assert set(r.excluded_lanes) == {"q1-p", "q1-s0", "q1-s1"}


# --- divergence is descriptive, never a score ----------------------------


def test_divergence_is_descriptive_only(run):
    r = build_report(run)
    d0 = next(d for d in r.divergences if d.shadow_id == "q1-s0")
    assert d0.point.diverged is True
    assert d0.point.node == "act2"
    assert d0.point.primary_tool == "fetch"
    assert d0.point.shadow_tool == "summarize"
    assert d0.point.within_first_two_steps is True
    assert "act2" in d0.sentence and "fetch" in d0.sentence and "summarize" in d0.sentence
    assert "%" not in d0.sentence                   # no score in the divergence text
    assert d0.overlap.multiset_jaccard == pytest.approx(1 / 5)
    assert d0.overlap.only_a == ("fetch",) and d0.overlap.only_b == ("summarize",)
    assert d0.arg_match.match_rate == 1.0
    assert d0.strict_match is False and d0.node_edge_match is False


def test_matching_shadow_shows_no_divergence(run):
    r = build_report(run)
    d1 = next(d for d in r.divergences if d.shadow_id == "q1-s1")
    assert d1.point.diverged is False
    assert d1.overlap.multiset_jaccard == 1.0
    assert d1.strict_match is True and d1.node_edge_match is True
    assert "same node trajectory" in d1.sentence


# --- rendering ---------------------------------------------------------


def test_render_sections_are_in_order(run):
    text = render_report(run)
    i_facts = text.index("1 · MEASURED FACTS")
    i_fid = text.index("2 · FIDELITY SUMMARY")
    i_div = text.index("3 · DIVERGENCE")
    assert i_facts < i_fid < i_div


def test_render_contains_the_key_lines(run):
    text = render_report(run)
    assert "SHADOW COMPARISON REPORT" in text
    assert "how many signups today" in text
    assert "price version amc-bundled-2026-02-01" in text
    assert "claude-opus-5" in text and "claude-sonnet-5" in text
    # fidelity block for the low-fidelity lane
    assert "LOW FIDELITY" in text
    # divergence rendered descriptively
    assert "first diverged from q1-p at node 'act2'" in text
    # measured vs substituted latency kept apart, never one blended figure
    assert "+subst" in text
    # no judgement layer: three sections only, none of them judged
    assert "JUDGED" not in text and "4 · " not in text


def test_render_no_blended_latency_for_substituted_lane(run):
    text = render_report(run)
    # q1-s0: 0 measured, 80ms substituted, 80ms total - reported as three fields
    line = next(l for l in text.splitlines() if l.strip().startswith("q1-s0")
                and "80" in l)
    assert "—" in line          # measured p50/p95 are dashes, not fabricated zeros


# --- edge cases ------------------------------------------------------


def test_report_without_primary_still_renders(run):
    store = SqliteStore(":memory:")
    store.write(
        queries=[QueryRow("q9", "x", "t0", False, "q9-s0")],
        lanes=[_lane("q9-s0", Role.SHADOW, query_id="q9", observed="claude-sonnet-5")],
        events=[_ev("q9-s0", 1, EventKind.TOOL, "search", "n", args=_args())],
    )
    r2 = load_query(store, "q9")
    rep = build_report(r2)
    assert rep.primary is None
    assert rep.divergences == ()
    text = render_report(r2)
    assert "No primary lane recorded" in text
    assert "not sampled" in text
    store.close()


def test_report_with_primary_only_has_no_divergence_section_body(run):
    store = SqliteStore(":memory:")
    store.write(
        queries=[QueryRow("q8", "x", "t0", True, "q8-p")],
        lanes=[_lane("q8-p", Role.PRIMARY, query_id="q8", observed="claude-opus-5")],
        events=[_ev("q8-p", 1, EventKind.TOOL, "search", "n", args=_args())],
    )
    r2 = load_query(store, "q8")
    text = render_report(r2)
    assert "No shadow lanes to compare." in text
    store.close()
