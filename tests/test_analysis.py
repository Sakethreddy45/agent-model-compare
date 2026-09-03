import json

import pytest

from amc.analysis import (
    BUNDLED_PRICES,
    MatchMode,
    ModelPrice,
    PriceTable,
    argument_match,
    cost_for_lane,
    describe_divergence,
    divergence_sentence,
    efficiency,
    fidelity_of,
    latency_profile,
    load_query,
    percentile,
    token_totals,
    tool_set_overlap,
    trajectory_match,
)
from amc.context import LaneStatus, Role
from amc.provenance import EventKind, LatencySource, ResponseSource
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
          contaminated=None):
    return LaneRow(
        lane_id=lane_id, query_id=query_id, role=role,
        model_requested=requested, model_observed=observed,
        status=LaneStatus.OK, error_type=None,
        started_at="t0", ended_at="t1", final_output="ok",
        contaminated_at_step=contaminated,
    )


@pytest.fixture
def run():
    """A hand-built fixture DB. Every metric below is checked against values
    computed by hand from exactly these rows."""
    store = SqliteStore(":memory:")
    store.write(
        queries=[QueryRow("q1", "how many signups", "t0", True, "q1-p")],
        lanes=[
            _lane("q1-p", Role.PRIMARY, observed="claude-opus-5"),
            _lane("q1-s0", Role.SHADOW,
                  requested="claude-sonnet-5", observed="claude-sonnet-5"),
        ],
        events=[
            # primary: search @act1, then fetch @act2 twice (a loop)
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
            # shadow: search @act1, diverges to summarize @act2, search @act3
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
        ],
    )
    yield load_query(store, "q1")
    store.close()


# --- loading -----------------------------------------------------------


def test_load_query_reconstructs_the_run(run):
    assert run.query.input == "how many signups"
    assert run.primary.lane_id == "q1-p"
    assert [l.lane_id for l in run.shadows] == ["q1-s0"]
    assert len(run.primary.tool_events) == 3
    assert len(run.primary.llm_events) == 2


# --- tool selection --------------------------------------------------


def test_tool_set_overlap(run):
    ov = tool_set_overlap(run.primary, run.shadows[0])
    assert ov.multiset_a == {"search": 1, "fetch": 2}
    assert ov.multiset_b == {"search": 2, "summarize": 1}
    assert ov.multiset_jaccard == pytest.approx(1 / 5)   # |∩|=1, |∪|=5
    assert ov.set_jaccard == pytest.approx(1 / 3)
    assert ov.only_a == ("fetch",)
    assert ov.only_b == ("summarize",)


def test_divergence_point_is_node_aligned_and_descriptive(run):
    dp = describe_divergence(run.primary, run.shadows[0])
    assert dp.diverged is True
    assert dp.alignment == "node"
    assert dp.node == "act2"
    assert dp.step == 2
    assert dp.primary_tool == "fetch"
    assert dp.shadow_tool == "summarize"
    assert dp.within_first_two_steps is True

    sentence = divergence_sentence(dp, "primary", "q1-s0")
    assert "act2" in sentence and "fetch" in sentence and "summarize" in sentence
    assert "%" not in sentence          # descriptive, never a score


def test_argument_match_rate(run):
    am = argument_match(run.primary, run.shadows[0])
    assert am.comparable == 1          # only act1 has the same tool in both lanes
    assert am.matched == 1
    assert am.match_rate == 1.0
    assert am.mismatches == ()


def test_trajectory_match_modes(run):
    p, s = run.primary, run.shadows[0]
    assert trajectory_match(p, s, MatchMode.STRICT) is False
    assert trajectory_match(p, s, MatchMode.UNORDERED) is False
    assert trajectory_match(p, s, MatchMode.SUBSET) is False
    assert trajectory_match(p, s, MatchMode.SUPERSET) is False
    assert trajectory_match(p, s, MatchMode.NODE_EDGES) is False
    assert trajectory_match(p, p, MatchMode.STRICT) is True
    assert trajectory_match(p, p, MatchMode.NODE_EDGES) is True


# --- efficiency ---------------------------------------------------


def test_efficiency_primary_has_a_loop_and_a_redundant_call(run):
    eff = efficiency(run.primary)
    assert eff.steps == 3
    assert eff.llm_turns == 2
    assert eff.distinct_tool_calls == 2        # search{q:x}, fetch{id:1}
    assert eff.redundant_calls == 1            # fetch{id:1} called twice
    assert eff.has_loop is True
    assert len(eff.loops) == 1
    assert eff.loops[0].tool == "fetch"
    assert eff.loops[0].length == 2
    assert eff.loops[0].start_seq == 4
    stats = {t.tool: t for t in eff.per_tool}
    assert stats["fetch"].calls == 2 and stats["fetch"].error_rate == 0.0


def test_efficiency_shadow_has_no_loop(run):
    eff = efficiency(run.shadows[0])
    assert eff.steps == 3
    assert eff.distinct_tool_calls == 3
    assert eff.redundant_calls == 0
    assert eff.has_loop is False


def test_retries_and_error_rate():
    store = SqliteStore(":memory:")
    store.write(
        queries=[QueryRow("q2", "x", "t0", True, "q2-p")],
        lanes=[_lane("q2-p", Role.PRIMARY, query_id="q2", observed="claude-opus-5")],
        events=[
            _ev("q2-p", 1, EventKind.TOOL, "call_api", "n", args=_args(u=1),
                err="TimeoutError"),
            _ev("q2-p", 2, EventKind.TOOL, "call_api", "n", args=_args(u=1)),   # retry
            _ev("q2-p", 3, EventKind.TOOL, "call_api", "n", args=_args(u=2)),
        ],
    )
    eff = efficiency(load_query(store, "q2").primary)
    (stats,) = eff.per_tool
    assert stats.calls == 3
    assert stats.errors == 1
    assert stats.retries == 1
    assert stats.error_rate == pytest.approx(1 / 3)
    store.close()


# --- cost -------------------------------------------------------


def test_token_totals(run):
    tp = token_totals(run.primary)
    assert (tp.tokens_in, tp.tokens_out, tp.cached_tokens) == (2500, 300, 200)
    assert tp.llm_calls == 2
    assert tp.complete is True


def test_cost_is_computed_from_tokens_and_a_price_version(run):
    table = PriceTable("test-v1", {
        "claude-opus-5": ModelPrice(10.0, 1.0, 30.0),
        "claude-sonnet-5": ModelPrice(3.0, 0.3, 15.0),
    })
    cb = cost_for_lane(run.primary, table)
    assert cb.priced is True
    assert cb.incomplete is False
    assert cb.price_version == "test-v1"
    # uncached_in = 2500-200 = 2300
    assert cb.input_cost == pytest.approx(2300 / 1e6 * 10.0)
    assert cb.cached_cost == pytest.approx(200 / 1e6 * 1.0)
    assert cb.output_cost == pytest.approx(300 / 1e6 * 30.0)
    assert cb.total_cost == pytest.approx(0.023 + 0.0002 + 0.009)

    cb_s = cost_for_lane(run.shadows[0], table)
    assert cb_s.total_cost == pytest.approx(2600 / 1e6 * 3.0 + 370 / 1e6 * 15.0)


def test_cost_unpriced_model_is_flagged_not_guessed(run):
    empty = PriceTable("no-prices", {})
    cb = cost_for_lane(run.primary, empty)
    assert cb.priced is False
    assert cb.total_cost is None
    assert cb.price_version == "no-prices"


def test_cost_incomplete_when_a_call_reports_no_tokens():
    store = SqliteStore(":memory:")
    store.write(
        queries=[QueryRow("q3", "x", "t0", True, "q3-p")],
        lanes=[_lane("q3-p", Role.PRIMARY, query_id="q3", observed="claude-opus-5")],
        events=[
            _ev("q3-p", 1, EventKind.LLM, "claude-opus-5", "n",
                tin=100, tout=10, cached=0),
            _ev("q3-p", 2, EventKind.LLM, "claude-opus-5", "n",
                tin=None, tout=None, cached=None),      # provider reported nothing
        ],
    )
    lane = load_query(store, "q3").primary
    tp = token_totals(lane)
    assert tp.missing_in == 1 and tp.complete is False
    cb = cost_for_lane(lane, BUNDLED_PRICES)
    assert cb.incomplete is True
    assert cb.total_cost is not None      # computed from the partial tokens, flagged
    store.close()


def test_bundled_price_table_has_a_version(run):
    cb = cost_for_lane(run.primary, BUNDLED_PRICES)
    assert cb.priced is True
    assert cb.price_version == "amc-bundled-2026-02-01"
    assert cb.total_cost > 0


# --- latency --------------------------------------------------


def test_percentile_is_nearest_rank():
    assert percentile([], 50) is None
    assert percentile([5.0], 95) == 5.0
    assert percentile([1, 2, 3, 4], 50) == 2       # rank ceil(2) = 2 -> xs[1]
    assert percentile([1, 2, 3, 4], 95) == 4       # rank ceil(3.8) = 4 -> xs[3]


def test_latency_profile_primary_all_measured(run):
    lp = latency_profile(run.primary)
    assert lp.measured_count == 3
    assert lp.measured_p50 == 30.0                 # sorted [30,30,40], rank 2
    assert lp.measured_p95 == 40.0
    assert lp.measured_total_ms == 100.0
    assert lp.substituted_total_ms == 0.0
    assert lp.combined_total_ms == 100.0
    assert lp.tool_total_ms == 100.0 and lp.llm_total_ms == 0.0
    assert lp.per_node_ms == (("act1", 40.0), ("act2", 60.0))
    assert lp.per_step_ms == (40.0, 30.0, 30.0)


def test_latency_profile_shadow_separates_measured_from_substituted(run):
    lp = latency_profile(run.shadows[0])
    assert lp.measured_count == 0
    assert lp.measured_p50 is None                 # nothing measured -> not a 0
    assert lp.measured_total_ms == 0.0
    assert lp.substituted_count == 2
    assert lp.substituted_total_ms == 80.0
    assert lp.combined_p50 == 40.0
    assert lp.combined_total_ms == 80.0
    assert lp.per_step_ms == (40.0, None, 40.0)    # the schema call has no latency


# --- fidelity meta-metrics ---------------------------------


def test_fidelity_of_shadow(run):
    fs = fidelity_of(run.shadows[0])
    assert fs.tool_calls == 3
    assert fs.executed_real == 0
    assert fs.from_fixture == 2
    assert fs.schema_synthesised == 1
    assert fs.latency_substituted_ms == 80.0
    assert fs.latency_measured_ms == 0.0
    assert fs.contaminated_at_step is None
    assert fs.is_low_fidelity() is True


# --- alignment fallback / no divergence --------------------


def _pair_without_nodes(a_tools, b_tools):
    store = SqliteStore(":memory:")
    store.write(
        queries=[QueryRow("qx", "x", "t0", True, "qx-p")],
        lanes=[_lane("qx-p", Role.PRIMARY, query_id="qx", observed="m"),
               _lane("qx-s0", Role.SHADOW, query_id="qx", observed="m")],
        events=[_ev("qx-p", i + 1, EventKind.TOOL, t, None, args=_args())
                for i, t in enumerate(a_tools)]
        + [_ev("qx-s0", i + 1, EventKind.TOOL, t, None, args=_args())
           for i, t in enumerate(b_tools)],
    )
    r = load_query(store, "qx")
    return r.primary, r.shadows[0], store


def test_divergence_falls_back_to_positional_without_node_names():
    p, s, store = _pair_without_nodes(["a", "b", "c"], ["a", "x", "c"])
    dp = describe_divergence(p, s)
    assert dp.alignment == "positional"
    assert dp.step == 2
    assert (dp.primary_tool, dp.shadow_tool) == ("b", "x")
    store.close()


def test_no_divergence_when_trajectories_match():
    p, s, store = _pair_without_nodes(["a", "b"], ["a", "b"])
    dp = describe_divergence(p, s)
    assert dp.diverged is False
    assert "same node trajectory" in divergence_sentence(dp, "primary", "shadow")
    assert argument_match(p, s).match_rate == 1.0
    store.close()
