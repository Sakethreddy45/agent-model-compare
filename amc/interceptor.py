from __future__ import annotations
import functools
import inspect
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from .context import current_lane
from .policy import Isolation, ToolPolicy

EVENTS: list[dict] = []          # kept for tests/observability; see also the sink


@dataclass(frozen=True)
class ToolEvent:
    """What the interceptor observed at the tool boundary. Deliberately not a
    store row - the interceptor sits below the recorder in the dependency
    order and must not import it. The recorder maps this onto an EventRow."""
    lane_id: str | None
    role: str
    name: str
    executed: bool
    reason: str
    isolation: Isolation | None
    latency_ms: float | None
    error_type: str | None
    args_json: str | None


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
    executed: bool,
    reason: str,
    policy: ToolPolicy | None,
    latency_ms: float | None,
    error_type: str | None,
    args_json: str | None,
) -> None:
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
            reason=reason,
            isolation=policy.isolation if policy is not None else None,
            latency_ms=latency_ms,
            error_type=error_type,
            args_json=args_json,
        ))


def _json_args(a: tuple, kw: dict) -> str:
    try:
        return json.dumps({"args": a, "kwargs": kw}, default=repr, sort_keys=True)
    except (TypeError, ValueError):
        return json.dumps({"repr": repr((a, kw))})


def _stub(name: str) -> dict[str, Any]:
    """Synthetic success. NEVER raise — an exception would make the agent
    branch differently and we'd be measuring our own interference."""
    return {"ok": True, "amc_stubbed": True, "tool": name}


def _decide(name: str, policy: ToolPolicy | None) -> tuple[bool, str]:
    """Returns (execute_for_real, reason)."""
    lane = current_lane()
    if lane is None:
        return False, "unknown_lane"          # unset != primary
    if lane.is_primary:
        return True, "primary"
    if policy is None:
        return False, "unclassified"
    if policy.isolation is Isolation.PASSTHROUGH:
        return True, f"passthrough:{policy.source}"
    return False, f"blocked:{policy.source}"


def wrap(fn: Callable, name: str, policies: dict[str, ToolPolicy]) -> Callable:
    policy = policies.get(name)

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(*a, **kw):
            go, why = _decide(name, policy)
            if not go:
                _emit(current_lane(), name, False, why, policy, None, None,
                      _json_args(a, kw))
                return _stub(name)
            t0 = time.perf_counter()
            try:
                out = await fn(*a, **kw)
            except Exception as exc:      # record then re-raise; never swallow
                _emit(current_lane(), name, True, why, policy,
                      (time.perf_counter() - t0) * 1000, type(exc).__name__,
                      _json_args(a, kw))
                raise
            _emit(current_lane(), name, True, why, policy,
                  (time.perf_counter() - t0) * 1000, None, _json_args(a, kw))
            return out
        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(*a, **kw):
        go, why = _decide(name, policy)
        if not go:
            _emit(current_lane(), name, False, why, policy, None, None,
                  _json_args(a, kw))
            return _stub(name)
        t0 = time.perf_counter()
        try:
            out = fn(*a, **kw)
        except Exception as exc:
            _emit(current_lane(), name, True, why, policy,
                  (time.perf_counter() - t0) * 1000, type(exc).__name__,
                  _json_args(a, kw))
            raise
        _emit(current_lane(), name, True, why, policy,
              (time.perf_counter() - t0) * 1000, None, _json_args(a, kw))
        return out
    return sync_wrapper
