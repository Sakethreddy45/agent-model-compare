import asyncio
import time
from types import SimpleNamespace

import pytest

from amc.context import Lane, LaneStatus, Role, current_lane, lane_scope
from amc.interceptor import wrap
from amc.policy import Isolation, classify
from amc.provider import ModelCollisionError, OpenAIAdapter
from amc.recorder import Recorder
from amc.runner import pending_shadow_tasks, shadow
from amc.store import EventKind, LatencySource, ResponseSource, SqliteStore


def _response(model, prompt=10, completion=4, cached=None):
    details = SimpleNamespace(cached_tokens=cached) if cached is not None else None
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            prompt_tokens_details=details,
        ),
    )


class DictStore:
    """A second `Store` implementation. If the recorder works against this
    unchanged, it depends only on the interface - SQLite can be swapped for
    Postgres without touching recorder.py."""

    def __init__(self):
        self.queries = {}
        self.lanes = {}
        self.events = {}
        self.closed = False
        self.write_calls = 0

    def write(self, *, queries=(), lanes=(), events=()):
        self.write_calls += 1
        for q in queries:
            self.queries[q.query_id] = q
        for lane in lanes:
            self.lanes[lane.lane_id] = lane
        for e in events:
            self.events.setdefault(e.lane_id, []).append(e)

    def read_query(self, query_id):
        return self.queries.get(query_id)

    def read_lanes(self, query_id):
        return [l for l in self.lanes.values() if l.query_id == query_id]

    def read_events(self, lane_id):
        return sorted(self.events.get(lane_id, []), key=lambda e: e.seq)

    def close(self):
        self.closed = True


async def _drain_shadows():
    tasks = pending_shadow_tasks()
    if tasks:
        return await asyncio.gather(*tasks, return_exceptions=True)
    return []


# --- store interface / buffering ------------------------------------------


@pytest.mark.asyncio
async def test_recorder_works_against_any_store_implementation():
    store = DictStore()
    async with Recorder(store, attach_interceptor_sink=False) as rec:
        rec.start_query("q1", input="hi", was_sampled=True, primary_lane_id="q1-p")
        lane = Lane("q1-p", Role.PRIMARY, None)
        rec.start_lane(lane, query_id="q1")
        rec.record_model_call(lane, OpenAIAdapter(), _response("gpt-4o"))
        rec.finish_lane(lane, status=LaneStatus.OK, final_output="done")
        await rec.flush()

    assert store.read_query("q1").input == "hi"
    assert store.read_lanes("q1")[0].model_observed == "gpt-4o"
    assert store.read_events("q1-p")[0].kind is EventKind.LLM
    assert store.closed is True


@pytest.mark.asyncio
async def test_writes_are_buffered_until_flush():
    store = DictStore()
    async with Recorder(store, flush_interval=1000, attach_interceptor_sink=False) as rec:
        rec.start_query("q1", input="x", was_sampled=False, primary_lane_id="q1-p")
        lane = Lane("q1-p", Role.PRIMARY, None)
        rec.start_lane(lane, query_id="q1")
        for _ in range(50):
            rec.record_model_call(lane, OpenAIAdapter(), _response("gpt-4o"))

        assert store.write_calls == 0          # nothing hit the store yet
        await rec.flush()
        assert store.write_calls == 1          # one batched write
        assert len(store.read_events("q1-p")) == 50


@pytest.mark.asyncio
async def test_recording_adds_no_latency_to_primary():
    async def bare_agent(x):
        await asyncio.sleep(0.03)
        return x

    async def recording_agent(x):
        await asyncio.sleep(0.03)
        lane = current_lane()
        for _ in range(50):   # a burst of recording on the primary's hot path
            rec.record_model_call(lane, OpenAIAdapter(), _response("gpt-4o"))
        return x

    store = DictStore()
    async with Recorder(store, flush_interval=1000, attach_interceptor_sink=False) as rec:
        plain = shadow([])(bare_agent)
        recorded = shadow([], recorder=rec)(recording_agent)

        t0 = time.perf_counter()
        await plain(1)
        t_plain = time.perf_counter() - t0

        t1 = time.perf_counter()
        await recorded(1)
        t_recorded = time.perf_counter() - t1

        # 50 model-call records + full lane lifecycle, all buffered: no SQLite
        # on the hot path, so the delta is microseconds, not a write's cost.
        assert t_recorded - t_plain < 0.02
        assert store.write_calls == 0   # nothing flushed synchronously


@pytest.mark.asyncio
async def test_primary_latency_unaffected_by_recorder_and_shadows():
    # Phase 2's latency test ran with no recorder. This is the same shape -
    # one agent, solo vs. full config - but "full" now means shadows AND an
    # attached recorder whose background flush loop is actively draining to a
    # real SQLite store (short flush_interval) while the primary runs.
    record = False

    async def agent(x):
        await asyncio.sleep(0.05)
        if record:
            lane = current_lane()
            model = "prod" if lane.role is Role.PRIMARY else lane.model
            for _ in range(20):   # real recording volume on the primary's path
                rec.record_model_call(lane, OpenAIAdapter(), _response(model))
        return x * 2

    store = SqliteStore(":memory:")
    async with Recorder(store, flush_interval=0.001, attach_interceptor_sink=False) as rec:
        solo = shadow([])(agent)
        full = shadow(["m1", "m2"], recorder=rec)(agent)

        t0 = time.perf_counter()
        r_solo = await solo(21)
        t_solo = time.perf_counter() - t0

        record = True
        t1 = time.perf_counter()
        r_full = await full(21)
        t_full = time.perf_counter() - t1

        await _drain_shadows()
        await rec.flush()

        assert r_solo == r_full == 42
        assert t_full - t_solo < 0.02

        # and the recorder genuinely captured that primary run concurrently -
        # this isn't fast because recording was skipped.
        prim = next(
            l for l in store.read_lanes(_all_lanes(store)[0].query_id)
            if l.role is Role.PRIMARY
        )
        assert prim.status is LaneStatus.OK
        assert len(store.read_events(prim.lane_id)) == 20


# --- full run reconstruction --------------------------------------------


@pytest.mark.asyncio
async def test_full_shadow_run_reconstructable_from_store():
    policies = classify(
        ["read_docs"],
        annotations={"read_docs": {"readOnlyHint": True, "destructiveHint": False}},
    )

    async def read_docs(q):
        return f"docs:{q}"

    docs = wrap(read_docs, "read_docs", policies)

    async def agent(q):
        lane = current_lane()
        model = "prod-model" if lane.role is Role.PRIMARY else lane.model
        rec.record_model_call(lane, OpenAIAdapter(), _response(model, prompt=100, completion=20))
        await docs(q)
        return f"answer for {q}"

    store = SqliteStore(":memory:")
    async with Recorder(store, flush_interval=1000) as rec:
        wrapped = shadow(["shadow-model"], recorder=rec)(agent)
        result = await wrapped("signups?")
        await _drain_shadows()
        await rec.flush()

        # reconstruct purely from the store
        queries = [
            store.read_query(qid)
            for qid in {l.query_id for l in _all_lanes(store)}
        ]
        assert result == "answer for signups?"
        (query,) = [q for q in queries if q is not None]
        assert query.input == "signups?"
        assert query.was_sampled is True

        lanes = {l.role: l for l in store.read_lanes(query.query_id)}
        assert lanes[Role.PRIMARY].status is LaneStatus.OK
        assert lanes[Role.PRIMARY].model_observed == "prod-model"
        assert lanes[Role.PRIMARY].final_output == "answer for signups?"
        assert lanes[Role.SHADOW].model_requested == "shadow-model"
        assert lanes[Role.SHADOW].model_observed == "shadow-model"

        for lane in lanes.values():
            kinds = [e.kind for e in store.read_events(lane.lane_id)]
            assert EventKind.LLM in kinds
            assert EventKind.TOOL in kinds
            seqs = [e.seq for e in store.read_events(lane.lane_id)]
            assert seqs == sorted(seqs) and seqs[0] == 1


def _all_lanes(store):
    # SqliteStore has no "list all" method; walk via a known query id space in
    # tests by reading the lanes table directly through its connection.
    rows = store._conn.execute("SELECT DISTINCT query_id FROM lanes").fetchall()
    out = []
    for r in rows:
        out.extend(store.read_lanes(r["query_id"]))
    return out


# --- interceptor sink --------------------------------------------------


@pytest.mark.asyncio
async def test_tool_events_flow_through_the_sink():
    policies = classify(
        ["read_docs", "send_email"],
        annotations={
            "read_docs": {"readOnlyHint": True, "destructiveHint": False},
            "send_email": {"readOnlyHint": False, "destructiveHint": True},
        },
    )

    async def read_docs(q):
        return "ok"

    async def send_email(to):
        return {"sent": to}

    docs = wrap(read_docs, "read_docs", policies)
    email = wrap(send_email, "send_email", policies)

    store = DictStore()
    async with Recorder(store, flush_interval=1000) as rec:
        rec.start_query("q1", input="x", was_sampled=True, primary_lane_id="q1-p")
        shadow_lane = Lane("q1-s", Role.SHADOW, "m")
        rec.start_lane(shadow_lane, query_id="q1")
        with lane_scope(shadow_lane):
            await docs("q")     # passthrough -> executed
            await email("a@b")  # blocked in a shadow -> stubbed
        await rec.flush()

    evs = {e.name: e for e in store.read_events("q1-s")}
    assert evs["read_docs"].blocked is False
    assert evs["read_docs"].response_source is ResponseSource.REAL
    assert evs["read_docs"].latency_source is LatencySource.MEASURED
    assert evs["read_docs"].latency_ms is not None
    assert evs["read_docs"].isolation_mode is Isolation.PASSTHROUGH

    assert evs["send_email"].blocked is True
    assert evs["send_email"].response_source is None
    assert evs["send_email"].latency_ms is None


# --- invariant 6 wired into the runner --------------------------------


@pytest.mark.asyncio
async def test_model_collision_detected_on_real_run():
    collisions = []

    async def agent(x):
        lane = current_lane()
        # primary and this shadow both end up observed as "gpt-4o"
        observed = "gpt-4o" if lane.model in (None, "collide") else lane.model
        rec.record_model_call(lane, OpenAIAdapter(), _response(observed))
        return x

    store = SqliteStore(":memory:")
    async with Recorder(store, flush_interval=1000, attach_interceptor_sink=False) as rec:
        wrapped = shadow(
            ["collide"],
            recorder=rec,
            on_model_collision=lambda lane, exc: collisions.append((lane.id, exc)),
        )(agent)
        await wrapped("q")
        await _drain_shadows()
        await rec.flush()

        assert len(collisions) == 1
        assert isinstance(collisions[0][1], ModelCollisionError)
        shadow_row = [
            l for l in store.read_lanes(_all_lanes(store)[0].query_id)
            if l.role is Role.SHADOW
        ][0]
        assert shadow_row.status is LaneStatus.INVALID


@pytest.mark.asyncio
async def test_model_collision_fails_loudly_by_default():
    async def agent(x):
        lane = current_lane()
        observed = "gpt-4o" if lane.model in (None, "collide") else lane.model
        rec.record_model_call(lane, OpenAIAdapter(), _response(observed))
        return x

    store = SqliteStore(":memory:")
    async with Recorder(store, flush_interval=1000, attach_interceptor_sink=False) as rec:
        wrapped = shadow(["collide"], recorder=rec)(agent)  # default handler re-raises
        await wrapped("q")
        results = await _drain_shadows()

    assert any(isinstance(r, ModelCollisionError) for r in results)


@pytest.mark.asyncio
async def test_distinct_models_produce_no_collision():
    async def agent(x):
        lane = current_lane()
        observed = "prod-model" if lane.role is Role.PRIMARY else lane.model
        rec.record_model_call(lane, OpenAIAdapter(), _response(observed))
        return x

    store = SqliteStore(":memory:")
    seen = []
    async with Recorder(store, flush_interval=1000, attach_interceptor_sink=False) as rec:
        wrapped = shadow(
            ["shadow-model"],
            recorder=rec,
            on_model_collision=lambda lane, exc: seen.append(exc),
        )(agent)
        await wrapped("q")
        results = await _drain_shadows()
        await rec.flush()

        assert seen == []
        assert not any(isinstance(r, ModelCollisionError) for r in results)
        lanes = {l.role: l for l in store.read_lanes(_all_lanes(store)[0].query_id)}
        assert lanes[Role.PRIMARY].model_observed == "prod-model"
        assert lanes[Role.SHADOW].model_observed == "shadow-model"
        assert lanes[Role.SHADOW].status is LaneStatus.OK
