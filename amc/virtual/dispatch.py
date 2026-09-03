from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from typing import Any, Literal

from ..provenance import LatencySource, ResponseSource
from .fixtures import FixtureStore, get_fixture_store
from .latency import replay_latency
from .overlay import Overlay, overlay_for
from .schema import synthesize

# The Virtual Tool: for a shadow's call to a destructive tool, produce a
# response that behaves like the real one - same shape, same lane-local state
# semantics, same timing - without the real consequence. Order of preference
# for the response: lane overlay (its own prior writes) -> fixture (what the
# primary saw) -> schema synthesis -> declared template -> bare stub.


@dataclass(frozen=True)
class VirtualSpec:
    """Per-tool knowledge the virtual layer needs beyond "it's destructive".
    All optional: with none of it, a call still resolves (fixture or stub) but
    the overlay can't link a write to a later read."""
    entity: str | None = None                      # links write tools to read tools
    op: Literal["read", "write"] = "write"
    id_field: str = "id"                            # identifier's key in the response
    id_arg: str | None = None                       # identifier's key in the call's kwargs
    output_schema: dict | None = None
    template: Any = None
    destructive: bool = True                        # feeds the contamination counter

    def key_for(self, ident: Any) -> str:
        return f"{self.entity or '_'}:{ident}"


@dataclass(frozen=True)
class VirtualMeta:
    response_source: ResponseSource
    latency_ms: float | None
    latency_source: LatencySource | None
    served_from_overlay: bool = False
    ungrounded_destructive: bool = False


_DEFAULT_SPEC = VirtualSpec()


def _clone(value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


def _extract_ident(args: tuple, kwargs: dict, spec: VirtualSpec) -> Any:
    for name in filter(None, (spec.id_arg, "id",
                              f"{spec.entity}_id" if spec.entity else None)):
        if name in kwargs:
            return kwargs[name]
    if len(args) == 1 and not kwargs:
        return args[0]
    return None


def _overlay_write(
    overlay: Overlay,
    spec: VirtualSpec,
    response: Any,
    kwargs: dict,
    source: ResponseSource,
) -> Any:
    """Record a write in the lane's delta and return the response the agent
    sees. If the response has no grounded identifier, mint a lane-scoped one so
    a later read can find the record."""
    if not isinstance(response, dict) or spec.entity is None:
        return response
    ident = response.get(spec.id_field)
    if ident in (None, "", 0):
        ident = overlay.new_id()
        response = {**response, spec.id_field: ident}
    record = {**response}
    for k, v in kwargs.items():
        record.setdefault(k, v)
    overlay.set(spec.key_for(ident), (record, source))
    return response


def resolve(
    tool_name: str,
    args: tuple,
    kwargs: dict,
    spec: VirtualSpec | None,
    *,
    lane_id: str,
    fixtures: FixtureStore | None = None,
) -> tuple[Any, VirtualMeta]:
    """Synchronous core: pick a response, apply overlay read/write, decide what
    latency to substitute. Does not sleep - `dispatch` does that."""
    spec = spec or _DEFAULT_SPEC
    fixtures = fixtures if fixtures is not None else get_fixture_store()
    overlay = overlay_for(lane_id)

    if spec.op == "read" and spec.entity is not None:
        ident = _extract_ident(args, kwargs, spec)
        if ident is not None:
            hit = overlay.get(spec.key_for(ident))
            if hit is not None:
                record, birth_source = hit
                fx = fixtures.lookup(tool_name, args, kwargs)
                lat, lat_src = replay_latency(fx)
                return _clone(record), VirtualMeta(
                    response_source=birth_source,
                    latency_ms=lat,
                    latency_source=lat_src,
                    served_from_overlay=True,
                )

    fx = fixtures.lookup(tool_name, args, kwargs)
    if fx is not None and fx.error is None:
        response = _clone(fx.response)
        if spec.op == "write":
            response = _overlay_write(overlay, spec, response, kwargs,
                                      ResponseSource.FIXTURE)
        lat, lat_src = replay_latency(fx)
        return response, VirtualMeta(
            response_source=ResponseSource.FIXTURE,
            latency_ms=lat,
            latency_source=lat_src,
        )

    if spec.output_schema is not None:
        response, source = synthesize(spec.output_schema), ResponseSource.SCHEMA
    elif spec.template is not None:
        response, source = _clone(spec.template), ResponseSource.TEMPLATE
    else:
        response = {"ok": True, "amc_virtual": True, "tool": tool_name}
        source = ResponseSource.STUB

    if spec.op == "write":
        response = _overlay_write(overlay, spec, response, kwargs, source)

    ungrounded = spec.destructive and source in (
        ResponseSource.SCHEMA, ResponseSource.TEMPLATE, ResponseSource.STUB
    )
    return response, VirtualMeta(
        response_source=source,
        latency_ms=None,
        latency_source=None,
        ungrounded_destructive=ungrounded,
    )


async def dispatch(
    tool_name: str,
    args: tuple,
    kwargs: dict,
    spec: VirtualSpec | None,
    *,
    lane_id: str,
    fixtures: FixtureStore | None = None,
) -> tuple[Any, VirtualMeta]:
    response, meta = resolve(tool_name, args, kwargs, spec,
                             lane_id=lane_id, fixtures=fixtures)
    if meta.latency_ms is not None:
        await asyncio.sleep(meta.latency_ms / 1000.0)
    return response, meta
