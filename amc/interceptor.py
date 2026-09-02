from __future__ import annotations
import functools
import inspect
from typing import Any, Callable

from .context import current_lane
from .policy import Isolation, ToolPolicy

EVENTS: list[dict] = []          # replaced by the recorder in a later phase


def _log(lane, name, executed, reason):
    EVENTS.append({
        "lane": lane.id if lane else None,
        "role": lane.role.value if lane else "unknown",
        "tool": name,
        "executed": executed,
        "reason": reason,
    })


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
            _log(current_lane(), name, go, why)
            return await fn(*a, **kw) if go else _stub(name)
        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(*a, **kw):
        go, why = _decide(name, policy)
        _log(current_lane(), name, go, why)
        return fn(*a, **kw) if go else _stub(name)
    return sync_wrapper