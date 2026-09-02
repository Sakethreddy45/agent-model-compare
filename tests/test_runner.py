import asyncio
import random
import time

import pytest

from amc.context import Role, current_lane
from amc.runner import pending_shadow_tasks, shadow


async def _drain_shadows():
    tasks = pending_shadow_tasks()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_primary_latency_unaffected_by_shadows():
    async def agent(x):
        await asyncio.sleep(0.05)
        return x * 2

    solo = shadow([])(agent)
    with_shadows = shadow(["gpt", "gemini"])(agent)   # default sample_rate: always shadowed

    t0 = time.perf_counter()
    r0 = await solo(21)
    t_solo = time.perf_counter() - t0

    t1 = time.perf_counter()
    r1 = await with_shadows(21)
    t_shadowed = time.perf_counter() - t1

    await _drain_shadows()

    assert r0 == r1 == 42
    assert t_shadowed - t_solo < 0.02   # spawning tasks costs a few ms, not another sleep


@pytest.mark.asyncio
async def test_shadow_failure_isolated_from_primary_and_siblings():
    completed = []
    errors = []

    async def agent(x):
        lane = current_lane()
        if lane.model == "flaky":
            raise RuntimeError("boom")
        completed.append(lane.model if lane else None)
        return x

    wrapped = shadow(
        ["flaky", "steady"],
        on_shadow_error=lambda lane, exc: errors.append((lane.id, exc)),
    )(agent)   # default sample_rate: always shadowed

    result = await wrapped(99)
    await _drain_shadows()

    assert result == 99                       # primary result unaffected
    assert "steady" in completed               # sibling shadow ran to completion
    assert len(errors) == 1
    assert isinstance(errors[0][1], RuntimeError)


@pytest.mark.asyncio
async def test_shadow_failure_does_not_propagate_to_caller():
    async def agent(x):
        if current_lane().role is Role.SHADOW:
            raise RuntimeError("boom")
        return x

    wrapped = shadow(["only-shadow"])(agent)   # default sample_rate: always shadowed

    result = await wrapped("fine")   # must not raise
    await _drain_shadows()
    assert result == "fine"


@pytest.mark.asyncio
async def test_default_sample_rate_always_shadows():
    spawned = []

    async def agent(x):
        lane = current_lane()
        if lane is not None and lane.role is Role.SHADOW:
            spawned.append(x)
        return x

    wrapped = shadow(["m1"])(agent)   # sample_rate not passed - must default to 1.0

    for i in range(20):
        await wrapped(i)
    await _drain_shadows()

    assert len(spawned) == 20


@pytest.mark.asyncio
async def test_fractional_sampling_rate():
    # Sampling is opt-in cost control, not the default - must be requested
    # explicitly via sample_rate.
    spawned = []

    async def agent(x):
        lane = current_lane()
        if lane is not None and lane.role is Role.SHADOW:
            spawned.append(x)
        return x

    rng = random.Random(1234)
    wrapped = shadow(["m1"], sample_rate=0.3, rng=rng)(agent)

    n = 500
    for i in range(n):
        await wrapped(i)
    await _drain_shadows()

    rate = len(spawned) / n
    assert 0.2 < rate < 0.4


@pytest.mark.asyncio
async def test_no_shadow_models_means_primary_only():
    async def agent(x):
        return x

    wrapped = shadow([])(agent)
    result = await wrapped(1)
    assert result == 1
    assert pending_shadow_tasks() == frozenset()


@pytest.mark.asyncio
async def test_enabled_false_skips_shadows_but_runs_primary_normally():
    primary_calls = []

    async def agent(x):
        lane = current_lane()
        # Primary must still be tagged, or the interceptor would treat it
        # as an unset/unknown lane and block its real side effects.
        assert lane is not None and lane.role is Role.PRIMARY
        primary_calls.append(x)
        return x * 2

    wrapped = shadow(["m1", "m2"], enabled=False)(agent)
    result = await wrapped(5)
    await _drain_shadows()

    assert result == 10
    assert primary_calls == [5]
    assert pending_shadow_tasks() == frozenset()


@pytest.mark.asyncio
async def test_amc_disabled_env_var_skips_shadows(monkeypatch):
    monkeypatch.setenv("AMC_DISABLED", "1")
    spawned = []

    async def agent(x):
        lane = current_lane()
        if lane is not None and lane.role is Role.SHADOW:
            spawned.append(x)
        return x

    wrapped = shadow(["m1"])(agent)   # enabled defaults True; env var still wins
    result = await wrapped(7)
    await _drain_shadows()

    assert result == 7
    assert spawned == []
    assert pending_shadow_tasks() == frozenset()


@pytest.mark.asyncio
async def test_amc_disabled_falsy_values_do_not_disable(monkeypatch):
    monkeypatch.setenv("AMC_DISABLED", "0")
    spawned = []

    async def agent(x):
        lane = current_lane()
        if lane is not None and lane.role is Role.SHADOW:
            spawned.append(x)
        return x

    wrapped = shadow(["m1"])(agent)
    await wrapped(1)
    await _drain_shadows()

    assert spawned == [1]
