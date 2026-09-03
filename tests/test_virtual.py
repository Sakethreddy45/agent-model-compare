import asyncio
import time

import pytest

from amc.context import Lane, LaneStatus, Role, lane_scope
from amc.fidelity import render, summarise
from amc.interceptor import wrap
from amc.policy import Isolation, classify
from amc.provenance import LatencySource, ResponseSource
from amc.recorder import Recorder
from amc.store import EventKind, SqliteStore
from amc.virtual import (
    FixtureStore,
    Overlay,
    VirtualSpec,
    discard_overlay,
    get_fixture_store,
    normalise_args,
    overlay_for,
    reset_fixture_store,
    reset_overlays,
    resolve,
    synthesize,
)


@pytest.fixture(autouse=True)
def _clean_virtual_state():
    reset_fixture_store()
    reset_overlays()
    yield
    reset_fixture_store()
    reset_overlays()


# --- fixture store -------------------------------------------------------


def test_normalise_args_is_key_order_independent():
    assert normalise_args((), {"a": 1, "b": 2}) == normalise_args((), {"b": 2, "a": 1})
    assert normalise_args((1, 2), {}) != normalise_args((2, 1), {})


def test_fixture_round_trip_and_ingest_copy():
    fs = FixtureStore()
    resp = {"id": 5, "nested": {"k": 1}}
    fs.record("t", (), {"x": 1}, response=resp, latency_ms=12.0)

    resp["id"] = 99                      # mutate after recording
    fx = fs.lookup("t", (), {"x": 1})
    assert fx.response == {"id": 5, "nested": {"k": 1}}
    assert fx.latency_ms == 12.0
    assert fs.lookup("t", (), {"x": 2}) is None


# --- schema synthesis --------------------------------------------------


def test_synthesize_object_with_types():
    out = synthesize({"type": "object", "properties": {
        "id": {"type": "string"},
        "n": {"type": "integer"},
        "ok": {"type": "boolean"},
        "tags": {"type": "array", "items": {"type": "string"}},
    }})
    assert out == {"id": "", "n": 0, "ok": False, "tags": [""]}


def test_synthesize_enum_default_and_none():
    assert synthesize({"enum": ["active", "closed"]}) == "active"
    assert synthesize({"type": "string", "default": "x"}) == "x"
    assert synthesize(None) is None


# --- overlay ----------------------------------------------------------


def test_overlay_reads_fall_through_writes_do_not():
    base = {"user:1": {"id": 1, "email": "real@x.com"}}
    ov = Overlay("laneA", base)

    assert ov.get("user:1")["email"] == "real@x.com"     # falls through to base
    ov.set("user:1", {"id": 1, "email": "shadow@x.com"})
    assert ov.get("user:1")["email"] == "shadow@x.com"   # delta wins
    assert base["user:1"]["email"] == "real@x.com"       # base untouched

    ov.delete("user:1")
    assert ov.get("user:1") is None                      # tombstone hides base
    assert "user:1" not in ov


def test_overlay_new_id_is_unique_and_lane_scoped():
    a, b = Overlay("laneA"), Overlay("laneB")
    assert a.new_id() != a.new_id()
    assert a.new_id().startswith("amc-laneA-")
    assert b.new_id().startswith("amc-laneB-")


def test_overlay_registry_discard_drops_the_delta():
    ov = overlay_for("L1")
    ov.set("k", "v")
    assert overlay_for("L1") is ov
    discard_overlay("L1")
    fresh = overlay_for("L1")
    assert fresh is not ov
    assert fresh.get("k") is None


# --- resolve: response source preference ----------------------------


def test_resolve_prefers_fixture_and_marks_replayed():
    fs = FixtureStore()
    fs.record("t", (), {"x": 1}, response={"id": "a"}, latency_ms=42.0)

    resp, meta = resolve("t", (), {"x": 1}, VirtualSpec(entity="e", op="write"),
                         lane_id="L", fixtures=fs)
    assert resp["id"] == "a"
    assert meta.response_source is ResponseSource.FIXTURE
    assert meta.latency_source is LatencySource.REPLAYED
    assert meta.latency_ms == 42.0
    assert meta.ungrounded_destructive is False


def test_resolve_falls_back_to_schema_then_stub():
    fs = FixtureStore()

    _, m_schema = resolve(
        "t", (), {}, VirtualSpec(op="write", entity="e", output_schema={
            "type": "object", "properties": {"id": {"type": "string"}}}),
        lane_id="L1", fixtures=fs)
    assert m_schema.response_source is ResponseSource.SCHEMA
    assert m_schema.latency_ms is None            # strategy (a) only: no observation, no guess
    assert m_schema.ungrounded_destructive is True

    _, m_stub = resolve("t2", (), {}, VirtualSpec(op="write", entity="e"),
                        lane_id="L2", fixtures=fs)
    assert m_stub.response_source is ResponseSource.STUB
    assert m_stub.ungrounded_destructive is True

    _, m_safe = resolve("t3", (1,), {}, VirtualSpec(op="read", entity="e", destructive=False),
                        lane_id="L3", fixtures=fs)
    assert m_safe.ungrounded_destructive is False


# --- pass condition 1: a shadow sees its own created user ----------


@pytest.mark.asyncio
async def test_shadow_calls_create_then_get_and_sees_its_own_user():
    real_calls: list = []

    async def create_user(email):
        real_calls.append(("create_user", email))
        return {"id": "real-db-id", "email": email, "status": "active"}

    async def get_user(user_id):
        real_calls.append(("get_user", user_id))
        return {"id": user_id, "email": "REAL-DB-VALUE", "status": "active"}

    policies = classify(["create_user", "get_user"],
                        config={"create_user": "virtual", "get_user": "virtual"})
    specs = {
        "create_user": VirtualSpec(entity="user", op="write", id_field="id",
                                   output_schema={"type": "object", "properties": {
                                       "id": {"type": "string"},
                                       "status": {"type": "string"}}}),
        "get_user": VirtualSpec(entity="user", op="read", id_arg="user_id"),
    }
    cu = wrap(create_user, "create_user", policies, specs=specs)
    gu = wrap(get_user, "get_user", policies, specs=specs)

    with lane_scope(Lane("shadow-1", Role.SHADOW, "m")):
        created = await cu(email="shadow@x.com")
        fetched = await gu(user_id=created["id"])

    assert real_calls == []                         # neither tool ran for real
    assert created["id"].startswith("amc-shadow-1-")  # lane-scoped synthetic id
    assert fetched["id"] == created["id"]
    assert fetched["email"] == "shadow@x.com"       # its own user, not REAL-DB-VALUE
    discard_overlay("shadow-1")


# --- pass condition 2: virtual latency tracks the primary's -------


@pytest.mark.asyncio
async def test_virtual_call_replays_the_primary_observed_latency():
    async def slow_write(payload):
        await asyncio.sleep(0.05)
        return {"id": "w1", "ok": True}

    policies = classify(["slow_write"], config={"slow_write": "virtual"})
    specs = {"slow_write": VirtualSpec(entity="thing", op="write", id_field="id")}
    tool = wrap(slow_write, "slow_write", policies, specs=specs)

    with lane_scope(Lane("p", Role.PRIMARY, None)):
        await tool(payload={"a": 1})                # real -> fixture captures ~50ms

    fx = get_fixture_store().lookup("slow_write", (), {"payload": {"a": 1}})
    assert fx is not None and fx.latency_ms >= 45

    t0 = time.perf_counter()
    with lane_scope(Lane("s", Role.SHADOW, "m")):
        await tool(payload={"a": 1})               # virtual -> replays the sleep
    elapsed = time.perf_counter() - t0

    assert 0.04 < elapsed < 0.25                   # slept ~50ms; did not run for real
    discard_overlay("s")


@pytest.mark.asyncio
async def test_lane_tool_latency_within_tolerance_of_primary_for_same_sequence():
    async def read_docs(q):
        await asyncio.sleep(0.002)
        return f"docs:{q}"

    async def create_user(email):
        await asyncio.sleep(0.03)
        return {"id": "u1", "email": email, "status": "active"}

    async def get_user(user_id):
        await asyncio.sleep(0.02)
        return {"id": user_id, "email": "REAL-DB-VALUE", "status": "active"}

    policies = classify(
        ["read_docs", "create_user", "get_user"],
        config={"create_user": "virtual", "get_user": "virtual"},
        annotations={"read_docs": {"readOnlyHint": True, "destructiveHint": False}},
    )
    specs = {
        "create_user": VirtualSpec(entity="user", op="write", id_field="id"),
        "get_user": VirtualSpec(entity="user", op="read", id_arg="user_id"),
    }
    rd = wrap(read_docs, "read_docs", policies, specs=specs)
    cu = wrap(create_user, "create_user", policies, specs=specs)
    gu = wrap(get_user, "get_user", policies, specs=specs)

    async def sequence():
        await rd("q")
        created = await cu(email="p@x.com")
        return await gu(user_id=created["id"])

    store = SqliteStore(":memory:")
    async with Recorder(store, flush_interval=1000, contamination_threshold=99) as rec:
        rec.start_query("q1", input="x", was_sampled=True, primary_lane_id="q1-p")
        primary = Lane("q1-p", Role.PRIMARY, None)
        shadow_lane = Lane("q1-s", Role.SHADOW, "m")
        rec.start_lane(primary, query_id="q1")
        rec.start_lane(shadow_lane, query_id="q1")

        with lane_scope(primary):
            await sequence()
        with lane_scope(shadow_lane):
            fetched = await sequence()

        rec.finish_lane(primary, status=LaneStatus.OK)
        rec.finish_lane(shadow_lane, status=LaneStatus.OK)
        await rec.flush()

        assert fetched["email"] == "p@x.com"        # overlay served it, not the fixture

        def tool_latency(lane_id):
            return sum(e.latency_ms or 0.0
                       for e in store.read_events(lane_id) if e.kind is EventKind.TOOL)

        assert abs(tool_latency("q1-s") - tool_latency("q1-p")) < 15.0  # ms

        srcs = {e.name: (e.response_source, e.latency_source)
                for e in store.read_events("q1-s") if e.kind is EventKind.TOOL}
        assert srcs["create_user"] == (ResponseSource.FIXTURE, LatencySource.REPLAYED)
        assert srcs["get_user"][1] is LatencySource.REPLAYED
        assert srcs["read_docs"] == (ResponseSource.REAL, LatencySource.MEASURED)
    discard_overlay("q1-s")
    discard_overlay("q1-p")


@pytest.mark.asyncio
async def test_shadow_diverging_to_an_unseen_tool_gets_no_latency_not_a_wrong_one():
    # The primary only ever calls `search`. The shadow takes a different path
    # and calls `fetch`, which the primary never touched - so there is no
    # observation to replay. Strategy (a) must yield None here, not borrow
    # `search`'s latency or invent a zero.
    async def search(q):
        await asyncio.sleep(0.04)
        return {"hits": ["d1", "d2"]}

    async def fetch(doc_id):
        await asyncio.sleep(0.04)          # slow for real - but the shadow never runs it
        return {"id": doc_id, "body": "REAL-CONTENT"}

    policies = classify(["search", "fetch"],
                        config={"search": "virtual", "fetch": "virtual"})
    specs = {
        "search": VirtualSpec(entity="query", op="read", destructive=False),
        "fetch": VirtualSpec(
            entity="doc", op="read", id_arg="doc_id", destructive=False,
            output_schema={"type": "object", "properties": {
                "id": {"type": "string"}, "body": {"type": "string"}}},
        ),
    }
    s_search = wrap(search, "search", policies, specs=specs)
    s_fetch = wrap(fetch, "fetch", policies, specs=specs)

    store = SqliteStore(":memory:")
    async with Recorder(store, flush_interval=1000, contamination_threshold=99) as rec:
        rec.start_query("q1", input="x", was_sampled=True, primary_lane_id="q1-p")
        primary = Lane("q1-p", Role.PRIMARY, None)
        shadow_lane = Lane("q1-s", Role.SHADOW, "m")
        rec.start_lane(primary, query_id="q1")
        rec.start_lane(shadow_lane, query_id="q1")

        with lane_scope(primary):
            await s_search(q="signups")        # primary's only call
        with lane_scope(shadow_lane):
            fetched = await s_fetch(doc_id="d1")   # divergent: primary never called fetch

        rec.finish_lane(primary, status=LaneStatus.OK)
        rec.finish_lane(shadow_lane, status=LaneStatus.OK)
        await rec.flush()

        # a `search` fixture exists with a real, non-trivial latency ...
        fx = get_fixture_store().lookup("search", (), {"q": "signups"})
        assert fx is not None and fx.latency_ms >= 35

        # ... and the divergent `fetch` call neither borrows it nor fakes a 0
        (fetch_ev,) = [e for e in store.read_events("q1-s")
                       if e.kind is EventKind.TOOL and e.name == "fetch"]
        assert fetch_ev.latency_ms is None
        assert fetch_ev.latency_source is None
        assert fetch_ev.latency_ms != fx.latency_ms
        assert fetch_ev.response_source is ResponseSource.SCHEMA
        assert fetched == {"id": "", "body": ""}      # synthesised, not REAL-CONTENT

        # the fidelity summary shows the substitution rather than hiding it
        shadow_row = next(l for l in store.read_lanes("q1") if l.role is Role.SHADOW)
        summary = summarise(shadow_row, store.read_events("q1-s"))
        assert summary.tool_calls == 1
        assert summary.schema_synthesised == 1
        assert summary.executed_real == 0
        assert summary.from_fixture == 0
        assert summary.latency_substituted_ms == 0.0   # nothing was legitimately replayed
        assert summary.latency_measured_ms == 0.0
        assert summary.real_fraction == 0.0
        assert summary.is_low_fidelity() is True

        text = render(summary)
        assert "schema-synthesised:" in text
        assert "LOW FIDELITY" in text
    discard_overlay("q1-s")
    discard_overlay("q1-p")


# --- contamination ------------------------------------------------


@pytest.mark.asyncio
async def test_ungrounded_destructive_calls_contaminate_the_lane():
    async def wipe(target):
        return {"done": True}

    policies = classify(["wipe"], config={"wipe": "virtual"})
    specs = {"wipe": VirtualSpec(entity="row", op="write")}   # no fixture, no schema -> STUB

    tool = wrap(wipe, "wipe", policies, specs=specs)
    store = SqliteStore(":memory:")
    async with Recorder(store, flush_interval=1000, contamination_threshold=2) as rec:
        rec.start_query("q1", input="x", was_sampled=True, primary_lane_id="q1-p")
        lane = Lane("q1-s", Role.SHADOW, "m")
        rec.start_lane(lane, query_id="q1")
        with lane_scope(lane):
            await tool(target="a")     # divergence 1
            await tool(target="b")     # divergence 2 -> contaminated at step 2
            await tool(target="c")     # already contaminated; step stays 2
        rec.finish_lane(lane, status=LaneStatus.OK)
        await rec.flush()

        row = next(l for l in store.read_lanes("q1") if l.role is Role.SHADOW)
        assert row.contaminated_at_step == 2
    discard_overlay("q1-s")


# --- primary independence / classification -----------------------


def test_classify_accepts_virtual_from_config():
    p = classify(["x"], config={"x": "virtual"})["x"]
    assert p.isolation is Isolation.VIRTUAL
    assert p.source == "config"


@pytest.mark.asyncio
async def test_primary_still_executes_virtual_tools_for_real():
    real_calls = []

    async def create_user(email):
        real_calls.append(email)
        return {"id": "u1", "email": email}

    policies = classify(["create_user"], config={"create_user": "virtual"})
    cu = wrap(create_user, "create_user", policies,
              specs={"create_user": VirtualSpec(entity="user", op="write")})

    with lane_scope(Lane("p", Role.PRIMARY, None)):
        out = await cu(email="p@x.com")

    assert real_calls == ["p@x.com"]     # virtual mode never blocks the primary
    assert out == {"id": "u1", "email": "p@x.com"}


@pytest.mark.asyncio
async def test_runner_discards_overlay_on_lane_teardown():
    from amc.runner import pending_shadow_tasks, shadow

    async def create_user(email):
        return {"id": "u1", "email": email}

    policies = classify(["create_user"], config={"create_user": "virtual"})
    cu = wrap(create_user, "create_user", policies,
              specs={"create_user": VirtualSpec(entity="user", op="write")})

    async def agent(x):
        return await cu(email="s@x.com")

    wrapped = shadow(["m1"])(agent)
    await wrapped("go")
    tasks = pending_shadow_tasks()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    from amc.virtual.overlay import _overlays
    assert _overlays == {}          # every lane's delta torn down
