import pytest

from amc.context import LaneStatus, Role
from amc.policy import Isolation
from amc.store import (
    EventKind,
    EventRow,
    LaneRow,
    LatencySource,
    QueryRow,
    ResponseSource,
    SqliteStore,
)


def _query(qid="q1"):
    return QueryRow(
        query_id=qid,
        input="how many users signed up yesterday?",
        created_at="2026-09-02T00:00:00+00:00",
        was_sampled=True,
        primary_lane_id=f"{qid}-primary",
    )


def _lane(qid="q1", lane_id="q1-primary", role=Role.PRIMARY, **kw):
    base = dict(
        lane_id=lane_id,
        query_id=qid,
        role=role,
        model_requested=None,
        model_observed=None,
        status=LaneStatus.RUNNING,
        error_type=None,
        started_at="2026-09-02T00:00:00+00:00",
        ended_at=None,
        final_output=None,
        contaminated_at_step=None,
    )
    base.update(kw)
    return LaneRow(**base)


def _event(lane_id="q1-primary", seq=1, **kw):
    base = dict(
        event_id=f"{lane_id}-e{seq}",
        lane_id=lane_id,
        seq=seq,
        node_name=None,
        kind=EventKind.LLM,
        name="gpt-4o",
        args_json=None,
        tokens_in=None,
        tokens_out=None,
        cached_tokens=None,
        latency_ms=None,
        latency_source=None,
        response_source=None,
        isolation_mode=None,
        blocked=False,
        error_type=None,
    )
    base.update(kw)
    return EventRow(**base)


@pytest.fixture
def store():
    s = SqliteStore(":memory:")
    yield s
    s.close()


def test_query_round_trip(store):
    store.write(queries=[_query()])
    got = store.read_query("q1")
    assert got == _query()
    assert store.read_query("missing") is None


def test_lane_upsert_merges_start_then_finish(store):
    store.write(lanes=[_lane()])
    store.write(lanes=[_lane(
        status=LaneStatus.OK,
        model_observed="gpt-4o-2024-11-20",
        ended_at="2026-09-02T00:00:05+00:00",
        final_output="42 users",
    )])

    lanes = store.read_lanes("q1")
    assert len(lanes) == 1
    assert lanes[0].status is LaneStatus.OK
    assert lanes[0].model_observed == "gpt-4o-2024-11-20"
    assert lanes[0].final_output == "42 users"
    assert lanes[0].started_at == "2026-09-02T00:00:00+00:00"


def test_null_is_distinct_from_zero(store):
    # tokens_in measured as 0, cached_tokens never reported; latency 0.0 vs None.
    store.write(events=[
        _event(seq=1, tokens_in=0, tokens_out=5, cached_tokens=None,
               latency_ms=0.0, latency_source=LatencySource.MEASURED),
        _event(seq=2, tokens_in=None, tokens_out=None, cached_tokens=0,
               latency_ms=None),
    ])
    a, b = store.read_events("q1-primary")

    assert a.tokens_in == 0 and a.tokens_in is not None
    assert a.cached_tokens is None
    assert a.latency_ms == 0.0 and a.latency_ms is not None

    assert b.tokens_in is None
    assert b.cached_tokens == 0 and b.cached_tokens is not None
    assert b.latency_ms is None
    assert b.latency_source is None


def test_event_enums_and_provenance_survive_round_trip(store):
    store.write(events=[_event(
        seq=1,
        kind=EventKind.TOOL,
        name="send_email",
        args_json='{"args": [], "kwargs": {"to": "x@y.com"}}',
        latency_ms=12.5,
        latency_source=LatencySource.MEASURED,
        response_source=ResponseSource.REAL,
        isolation_mode=Isolation.PASSTHROUGH,
        blocked=False,
    )])
    (ev,) = store.read_events("q1-primary")
    assert ev.kind is EventKind.TOOL
    assert ev.latency_source is LatencySource.MEASURED
    assert ev.response_source is ResponseSource.REAL
    assert ev.isolation_mode is Isolation.PASSTHROUGH
    assert ev.blocked is False


def test_blocked_event_has_null_response_source(store):
    store.write(events=[_event(
        seq=1, kind=EventKind.TOOL, name="mystery_tool",
        blocked=True, response_source=None, latency_ms=None,
    )])
    (ev,) = store.read_events("q1-primary")
    assert ev.blocked is True
    assert ev.response_source is None


def test_full_run_reconstructable_from_store_alone(store):
    store.write(
        queries=[_query()],
        lanes=[
            _lane(lane_id="q1-primary", role=Role.PRIMARY, status=LaneStatus.OK),
            _lane(lane_id="q1-shadow-0", role=Role.SHADOW,
                  model_requested="claude-sonnet-5", status=LaneStatus.OK),
        ],
        events=[
            _event(lane_id="q1-primary", seq=1, tokens_in=100, tokens_out=20),
            _event(lane_id="q1-shadow-0", seq=1, tokens_in=110, tokens_out=25),
        ],
    )

    q = store.read_query("q1")
    lanes = {l.lane_id: l for l in store.read_lanes("q1")}
    assert q is not None
    assert set(lanes) == {"q1-primary", "q1-shadow-0"}
    assert lanes["q1-primary"].role is Role.PRIMARY
    assert lanes["q1-shadow-0"].role is Role.SHADOW
    assert len(store.read_events("q1-primary")) == 1
    assert len(store.read_events("q1-shadow-0")) == 1


def test_write_batch_is_atomic(store):
    store.write(queries=[_query()])
    bad = EventRow(**{**_event(seq=1).__dict__, "kind": None})  # kind.value -> AttributeError
    with pytest.raises(Exception):
        store.write(
            lanes=[_lane(lane_id="q1-primary", status=LaneStatus.OK)],
            events=[bad],
        )
    # the lane write in the same batch must have rolled back
    assert store.read_lanes("q1") == []


def test_sqlite_file_backend_persists(tmp_path):
    path = tmp_path / "run.db"
    s1 = SqliteStore(path)
    s1.write(queries=[_query()])
    s1.close()

    s2 = SqliteStore(path)
    assert s2.read_query("q1") == _query()
    s2.close()
