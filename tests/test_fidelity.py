from amc.context import LaneStatus, Role
from amc.fidelity import render, summarise
from amc.provenance import EventKind, LatencySource, ResponseSource
from amc.store import EventRow, LaneRow


def _lane(**kw):
    base = dict(
        lane_id="q1-shadow-0",
        query_id="q1",
        role=Role.SHADOW,
        model_requested="claude-sonnet-5",
        model_observed="claude-sonnet-5",
        status=LaneStatus.OK,
        error_type=None,
        started_at="t0",
        ended_at="t1",
        final_output="ok",
        contaminated_at_step=None,
    )
    base.update(kw)
    return LaneRow(**base)


def _ev(seq, *, kind=EventKind.TOOL, blocked=False, response_source=None,
        latency_ms=None, latency_source=None):
    return EventRow(
        event_id=f"e{seq}", lane_id="q1-shadow-0", seq=seq, node_name=None,
        kind=kind, name="tool", args_json=None,
        tokens_in=None, tokens_out=None, cached_tokens=None,
        latency_ms=latency_ms, latency_source=latency_source,
        response_source=response_source, isolation_mode=None,
        blocked=blocked, error_type=None,
    )


def test_summarise_counts_each_response_source():
    events = [
        _ev(1, response_source=ResponseSource.REAL,
            latency_ms=100.0, latency_source=LatencySource.MEASURED),
        _ev(2, response_source=ResponseSource.FIXTURE,
            latency_ms=40.0, latency_source=LatencySource.REPLAYED),
        _ev(3, response_source=ResponseSource.SCHEMA),
        _ev(4, response_source=ResponseSource.STUB),
        _ev(5, blocked=True),
        _ev(6, kind=EventKind.LLM, latency_ms=999.0),   # not a tool call - ignored
    ]
    s = summarise(_lane(), events)

    assert s.tool_calls == 5
    assert s.executed_real == 1
    assert s.from_fixture == 1
    assert s.schema_synthesised == 1
    assert s.stubbed == 1
    assert s.blocked == 1
    assert s.latency_measured_ms == 100.0
    assert s.latency_substituted_ms == 40.0
    assert s.latency_total_ms == 140.0
    assert round(s.latency_substituted_fraction, 3) == round(40 / 140, 3)
    assert s.contaminated_at_step is None


def test_summarise_flags_low_fidelity_when_mostly_substituted():
    events = [_ev(i, response_source=ResponseSource.SCHEMA) for i in range(1, 5)]
    events.append(_ev(5, response_source=ResponseSource.REAL))
    s = summarise(_lane(), events)
    assert s.real_fraction == 0.2
    assert s.is_low_fidelity() is True


def test_summarise_flags_low_fidelity_when_contaminated():
    events = [_ev(i, response_source=ResponseSource.REAL) for i in range(1, 6)]
    s = summarise(_lane(contaminated_at_step=3), events)
    assert s.real_fraction == 1.0
    assert s.is_low_fidelity() is True          # contamination alone is enough


def test_render_produces_the_doc_shaped_block():
    events = [
        _ev(1, response_source=ResponseSource.REAL,
            latency_ms=100.0, latency_source=LatencySource.MEASURED),
        _ev(2, response_source=ResponseSource.FIXTURE,
            latency_ms=40.0, latency_source=LatencySource.REPLAYED),
        _ev(3, response_source=ResponseSource.SCHEMA),
    ]
    text = render(summarise(_lane(), events))

    assert "lane: q1-shadow-0 (claude-sonnet-5)" in text
    assert "tool calls:" in text
    assert "executed for real:" in text
    assert "served from fixture:" in text
    assert "schema-synthesised:" in text
    assert "latency substituted: 40ms of 140ms" in text
    assert "contaminated:        no" in text


def test_render_shows_contamination_step():
    events = [_ev(1, response_source=ResponseSource.STUB)]
    text = render(summarise(_lane(contaminated_at_step=1), events))
    assert "contaminated:        at step 1" in text
    assert "LOW FIDELITY" in text
