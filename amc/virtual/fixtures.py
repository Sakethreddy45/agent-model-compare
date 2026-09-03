from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass
from typing import Any

# The fixture store is the primary lane's execution captured as replayable
# facts: for each (tool, normalised args) call the primary made, what came
# back, how long it took, and whether it errored. Shadows are served from
# this rather than from anything we invent.
#
#   Capture from the primary. Replay to the shadows.


def normalise_args(args: tuple, kwargs: dict) -> str:
    """A canonical key for a call. `{"a":1,"b":2}` and `{"b":2,"a":1}` are the
    same call - sort keys, strip incidental whitespace, format floats one way -
    or identical calls read as cache misses."""
    payload = {"args": list(args), "kwargs": kwargs}
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          default=repr)
    except (TypeError, ValueError):
        return json.dumps({"repr": repr(payload)})


@dataclass(frozen=True)
class Fixture:
    response: Any
    latency_ms: float | None
    error: str | None
    observed_at: float


class FixtureStore:
    """`(tool_name, normalised_args) -> Fixture`. Process-wide; the primary
    writes, shadows read."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], Fixture] = {}
        self._by_tool: dict[str, list[Fixture]] = {}

    def record(
        self,
        tool_name: str,
        args: tuple,
        kwargs: dict,
        *,
        response: Any,
        latency_ms: float | None,
        error: str | None = None,
    ) -> None:
        fx = Fixture(
            response=_clone(response),
            latency_ms=latency_ms,
            error=error,
            observed_at=time.time(),
        )
        self._by_key[(tool_name, normalise_args(args, kwargs))] = fx
        self._by_tool.setdefault(tool_name, []).append(fx)

    def lookup(self, tool_name: str, args: tuple, kwargs: dict) -> Fixture | None:
        return self._by_key.get((tool_name, normalise_args(args, kwargs)))

    def observations(self, tool_name: str) -> list[Fixture]:
        """Every fixture recorded for a tool, regardless of args. Strategy (a)
        replay never uses this - it's here for later strategies."""
        return list(self._by_tool.get(tool_name, ()))

    def clear(self) -> None:
        self._by_key.clear()
        self._by_tool.clear()


def _clone(value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception:      # unpicklable handle - store the reference as-is
        return value


_store = FixtureStore()


def get_fixture_store() -> FixtureStore:
    return _store


def set_fixture_store(store: FixtureStore) -> None:
    global _store
    _store = store


def reset_fixture_store() -> None:
    _store.clear()
