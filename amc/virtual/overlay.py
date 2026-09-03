from __future__ import annotations

from typing import Any, Iterator, Mapping

# Per-lane copy-on-write state. This is what makes "the agent's next step
# breaks" go away: a shadow that calls create_user then get_user sees its own
# user, so it proceeds exactly as it would have.
#
#   Read:  check the lane's delta first; if absent, fall through to real state.
#   Write: record in the delta only. Never touch real state.
#   Teardown: discard the delta when the lane completes.
#
# Isolation here is logical, not physical - a tool that shells out to a
# subprocess, or holds its own unwrapped client, bypasses this.

_MISSING = object()


class Overlay:
    def __init__(self, lane_id: str, base: Mapping[str, Any] | None = None) -> None:
        self._lane_id = lane_id
        self._base: Mapping[str, Any] = base if base is not None else {}
        self._delta: dict[str, Any] = {}
        self._tombstones: set[str] = set()
        self._id_counter = 0

    def get(self, key: str) -> Any:
        if key in self._tombstones:
            return None
        if key in self._delta:
            return self._delta[key]
        val = self._base.get(key, _MISSING) if isinstance(self._base, Mapping) else _MISSING
        return None if val is _MISSING else val

    def __contains__(self, key: str) -> bool:
        if key in self._tombstones:
            return False
        return key in self._delta or key in self._base

    def set(self, key: str, value: Any) -> None:
        self._tombstones.discard(key)
        self._delta[key] = value

    def delete(self, key: str) -> None:
        self._delta.pop(key, None)
        self._tombstones.add(key)

    def new_id(self) -> str:
        """A shadow-scoped synthetic id in a reserved namespace. The string
        prefix keeps it from colliding with a real integer id, and per-lane
        numbering keeps two shadows' ids apart."""
        self._id_counter += 1
        return f"amc-{self._lane_id}-{self._id_counter}"

    def keys(self) -> Iterator[str]:
        seen = set()
        for k in self._delta:
            if k not in self._tombstones:
                seen.add(k)
                yield k
        for k in self._base:
            if k not in self._tombstones and k not in seen:
                yield k


_overlays: dict[str, Overlay] = {}
_base: Mapping[str, Any] | None = None


def set_overlay_base(base: Mapping[str, Any] | None) -> None:
    """The read-only real state shadows' reads fall through to. Defaults to
    empty - wire a real store here when you have one."""
    global _base
    _base = base


def overlay_for(lane_id: str) -> Overlay:
    ov = _overlays.get(lane_id)
    if ov is None:
        ov = Overlay(lane_id, _base)
        _overlays[lane_id] = ov
    return ov


def has_overlay(lane_id: str) -> bool:
    return lane_id in _overlays


def discard_overlay(lane_id: str) -> None:
    """Teardown: drop the lane's delta. Safe to call for a lane that never
    created one."""
    _overlays.pop(lane_id, None)


def reset_overlays() -> None:
    _overlays.clear()
