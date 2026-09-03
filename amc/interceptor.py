from __future__ import annotations
import functools
import inspect
import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from .context import current_lane
from .policy import Isolation, ToolPolicy
from .provenance import LatencySource, ResponseSource
from .virtual import FixtureStore, VirtualSpec, get_fixture_store, resolve
from .virtual.dispatch import dispatch

EVENTS: list[dict] = []          # kept for tests/observability; see also the sink


class _Action(str, Enum):
    REAL = "real"       # execute the tool for real
    VIRTUAL = "virtual"  # route through the virtual tool layer
    STUB = "stub"        # hard block: synthetic success, tool never touched


@dataclass(frozen=True)
class ToolEvent:
    """What the interceptor observed at the tool boundary. Deliberately not a
    store row - the interceptor sits below the recorder in the dependency
    order and must not import it. The recorder maps this onto an EventRow."""
    lane_id: str | None
    role: str
    name: str
    executed: bool                       # ran for real
    blocked: bool                        # hard-blocked (STUB)
    reason: str
    isolation: Isolation | None
    latency_ms: float | None
    latency_source: LatencySource | None
    response_source: ResponseSource | None
    error_type: str | None
    args_json: str | None
    ungrounded_destructive: bool = False


_Sink = Callable[[ToolEvent], None]
_SINK: _Sink | None = None


def set_sink(sink: _Sink | None) -> None:
    """Register a single consumer for tool events (the recorder). Module-level
    because the interceptor wraps tools once at startup and has no per-run
    handle; one recorder per process. Pass None to detach."""
    global _SINK
    _SINK = sink


def _emit(
    lane,
    name: str,
    action: _Action,
    reason: str,
    isolation: Isolation | None,
    *,
    latency_ms: float | None = None,
    latency_source: LatencySource | None = None,
    response_source: ResponseSource | None = None,
    error_type: str | None = None,
    args_json: str | None = None,
    ungrounded_destructive: bool = False,
) -> None:
    executed = action is _Action.REAL
    EVENTS.append({
        "lane": lane.id if lane else None,
        "role": lane.role.value if lane else "unknown",
        "tool": name,
        "executed": executed,
        "reason": reason,
    })
    if _SINK is not None:
        _SINK(ToolEvent(
            lane_id=lane.id if lane else None,
            role=lane.role.value if lane else "unknown",
            name=name,
            executed=executed,
            blocked=action is _Action.STUB,
            reason=reason,
            isolation=isolation,
            latency_ms=latency_ms,
            latency_source=latency_source,
            response_source=response_source,
            error_type=error_type,
            args_json=args_json,
            ungrounded_destructive=ungrounded_destructive,
        ))


def _json_args(a: tuple, kw: dict) -> str:
    try:
        return json.dumps({"args": a, "kwargs": kw}, default=repr, sort_keys=True)
    except (TypeError, ValueError):
        return json.dumps({"repr": repr((a, kw))})


def _stub(name: str) -> dict[str, Any]:
    """Synthetic success for a hard-blocked tool. NEVER raise — an exception
    would make the agent branch differently and we'd be measuring our own
    interference."""
    return {"ok": True, "amc_stubbed": True, "tool": name}


def _decide(name: str, policy: ToolPolicy | None) -> tuple[_Action, str]:
    lane = current_lane()
    if lane is None:
        return _Action.STUB, "unknown_lane"       # unset != primary
    if lane.is_primary:
        return _Action.REAL, "primary"
    if policy is None:
        return _Action.STUB, "unclassified"
    if policy.isolation is Isolation.PASSTHROUGH:
        return _Action.REAL, f"passthrough:{policy.source}"
    if policy.isolation is Isolation.VIRTUAL:
        return _Action.VIRTUAL, f"virtual:{policy.source}"
    return _Action.STUB, f"blocked:{policy.source}"


def wrap(
    fn: Callable,
    name: str,
    policies: dict[str, ToolPolicy],
    *,
    specs: dict[str, VirtualSpec] | None = None,
    fixtures: FixtureStore | None = None,
) -> Callable:
    policy = policies.get(name)
    spec = (specs or {}).get(name)
    iso = policy.isolation if policy is not None else None
    store = fixtures if fixtures is not None else get_fixture_store()

    def _capture_primary(lane, a, kw, response, latency_ms):
        if lane is not None and lane.is_primary:
            store.record(name, a, kw, response=response, latency_ms=latency_ms)

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(*a, **kw):
            lane = current_lane()
            action, why = _decide(name, policy)

            if action is _Action.STUB:
                _emit(lane, name, action, why, iso, args_json=_json_args(a, kw))
                return _stub(name)

            if action is _Action.VIRTUAL:
                response, meta = await dispatch(
                    name, a, kw, spec, lane_id=lane.id, fixtures=store
                )
                _emit(lane, name, action, why, iso,
                      latency_ms=meta.latency_ms,
                      latency_source=meta.latency_source,
                      response_source=meta.response_source,
                      args_json=_json_args(a, kw),
                      ungrounded_destructive=meta.ungrounded_destructive)
                return response

            t0 = time.perf_counter()
            try:
                out = await fn(*a, **kw)
            except Exception as exc:      # record then re-raise; never swallow
                _emit(lane, name, action, why, iso,
                      latency_ms=(time.perf_counter() - t0) * 1000,
                      error_type=type(exc).__name__, args_json=_json_args(a, kw))
                raise
            elapsed = (time.perf_counter() - t0) * 1000
            _capture_primary(lane, a, kw, out, elapsed)
            _emit(lane, name, action, why, iso,
                  latency_ms=elapsed, latency_source=LatencySource.MEASURED,
                  response_source=ResponseSource.REAL, args_json=_json_args(a, kw))
            return out
        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(*a, **kw):
        lane = current_lane()
        action, why = _decide(name, policy)

        if action is _Action.STUB:
            _emit(lane, name, action, why, iso, args_json=_json_args(a, kw))
            return _stub(name)

        if action is _Action.VIRTUAL:
            # No event loop to await on: resolve the response and overlay
            # synchronously, but the latency replay sleep is skipped. A sync
            # tool in virtual mode gets the right response, not the right
            # wall-clock. Prefer async tools where timing fidelity matters.
            response, meta = resolve(
                name, a, kw, spec, lane_id=lane.id, fixtures=store
            )
            _emit(lane, name, action, why, iso,
                  latency_ms=meta.latency_ms,
                  latency_source=meta.latency_source,
                  response_source=meta.response_source,
                  args_json=_json_args(a, kw),
                  ungrounded_destructive=meta.ungrounded_destructive)
            return response

        t0 = time.perf_counter()
        try:
            out = fn(*a, **kw)
        except Exception as exc:
            _emit(lane, name, action, why, iso,
                  latency_ms=(time.perf_counter() - t0) * 1000,
                  error_type=type(exc).__name__, args_json=_json_args(a, kw))
            raise
        elapsed = (time.perf_counter() - t0) * 1000
        _capture_primary(lane, a, kw, out, elapsed)
        _emit(lane, name, action, why, iso,
              latency_ms=elapsed, latency_source=LatencySource.MEASURED,
              response_source=ResponseSource.REAL, args_json=_json_args(a, kw))
        return out
    return sync_wrapper
