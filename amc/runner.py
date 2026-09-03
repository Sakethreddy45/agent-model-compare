from __future__ import annotations
import asyncio
import functools
import os
import random
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, Sequence

from .context import Lane, LaneStatus, Role, lane_scope
from .provider import ModelCollisionError, assert_distinct_from_primary
from .virtual import discard_overlay

ShadowErrorHandler = Callable[[Lane, Exception], Any]
ModelCollisionHandler = Callable[[Lane, ModelCollisionError], Any]
DescribeInput = Callable[[tuple, dict], str]

# Keep strong refs so fire-and-forget tasks aren't GC'd mid-flight
# (asyncio only holds a weak reference once nothing else does).
_background_tasks: set[asyncio.Task] = set()

_ENV_DISABLE_VAR = "AMC_DISABLED"
_ENV_FALSY = {"", "0", "false", "no", "off"}


class RunRecorder(Protocol):
    """What `@shadow` needs from a recorder. `recorder.Recorder` satisfies it
    structurally; the runner never imports the recorder module (it sits above
    the runner in the dependency order) - the caller passes an already-started
    instance in."""

    def start_query(
        self, query_id: str, *, input: str, was_sampled: bool, primary_lane_id: str
    ) -> None: ...

    def start_lane(self, lane: Lane, *, query_id: str) -> None: ...

    def finish_lane(
        self,
        lane: Lane,
        *,
        status: LaneStatus,
        final_output: str | None = None,
        error_type: str | None = None,
        contaminated_at_step: int | None = None,
    ) -> None: ...

    def model_observed(self, lane_id: str) -> str | None: ...

    def mark_collision(self, lane_id: str) -> None: ...


def _env_disabled() -> bool:
    # Read fresh on every call, not once at decoration time, so ops can flip
    # this in a running process (or a test can monkeypatch it) with no redeploy.
    return os.environ.get(_ENV_DISABLE_VAR, "").strip().lower() not in _ENV_FALSY


def _default_on_shadow_error(lane: Lane, exc: Exception) -> None:
    print(f"[amc] shadow lane {lane.id} ({lane.model}) raised "
          f"{type(exc).__name__}: {exc}")


def _default_on_model_collision(lane: Lane, exc: ModelCollisionError) -> None:
    # Invariant 6 is the silent failure that invalidates everything, so the
    # default is to fail loudly: print, then re-raise into the shadow task so
    # asyncio surfaces it rather than let the run look clean.
    print(f"[amc] INVARIANT 6 VIOLATED: {exc}", file=sys.stderr)
    raise exc


def _default_describe_input(args: tuple, kwargs: dict) -> str:
    if args:
        return args[0] if isinstance(args[0], str) else repr(args[0])
    if kwargs:
        return repr(kwargs)
    return ""


def _stringify(value: Any) -> str:
    return value if isinstance(value, str) else repr(value)


@dataclass(frozen=True)
class _ShadowSpawn:
    fn: Callable[..., Awaitable[Any]]
    lane: Lane
    args: tuple
    kwargs: dict
    on_error: ShadowErrorHandler
    on_collision: ModelCollisionHandler
    recorder: RunRecorder | None
    primary_lane: Lane


def _enforce_model_distinct(s: _ShadowSpawn) -> None:
    """Invariant 6, wired to fire on every real shadow run. Deliberately called
    outside the invariant-2 exception guard in `_run_shadow`: a collision must
    not be swallowed the way an ordinary shadow failure is."""
    rec = s.recorder
    if rec is None:
        return
    try:
        assert_distinct_from_primary(
            primary_observed=rec.model_observed(s.primary_lane.id),
            shadow_observed=rec.model_observed(s.lane.id),
            shadow_lane=s.lane,
        )
    except ModelCollisionError as exc:
        rec.mark_collision(s.lane.id)
        s.on_collision(s.lane, exc)


async def _run_shadow(s: _ShadowSpawn) -> None:
    """Isolated per invariant 2: a raise inside the agent call must reach no one
    but this lane's own error handler. Primary and sibling shadows are
    untouched. The invariant 6 check afterwards is intentionally not guarded."""
    try:
        try:
            with lane_scope(s.lane):
                result = await s.fn(*s.args, **s.kwargs)
        except Exception as exc:      # noqa: BLE001 - deliberate shadow guard
            if s.recorder is not None:
                s.recorder.finish_lane(
                    s.lane, status=LaneStatus.ERROR, error_type=type(exc).__name__
                )
            s.on_error(s.lane, exc)
            return

        if s.recorder is not None:
            s.recorder.finish_lane(
                s.lane, status=LaneStatus.OK, final_output=_stringify(result)
            )
        _enforce_model_distinct(s)
    finally:
        discard_overlay(s.lane.id)   # teardown: drop the lane's copy-on-write delta


def _spawn_shadow(s: _ShadowSpawn) -> asyncio.Task:
    # lane_scope is entered inside _run_shadow, i.e. inside the task's own
    # coroutine, never here - contextvars must be set after spawning.
    task = asyncio.create_task(_run_shadow(s))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def pending_shadow_tasks() -> frozenset[asyncio.Task]:
    """In-flight shadow tasks. Exists for tests/observability to await
    fire-and-forget work; production callers should never need this."""
    return frozenset(_background_tasks)


def shadow(
    models: Sequence[str],
    *,
    sample_rate: float = 1.0,
    primary_model: str | None = None,
    on_shadow_error: ShadowErrorHandler = _default_on_shadow_error,
    on_model_collision: ModelCollisionHandler = _default_on_model_collision,
    rng: random.Random | None = None,
    enabled: bool = True,
    recorder: RunRecorder | None = None,
    describe_input: DescribeInput = _default_describe_input,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Wrap an agent's entry point. The primary lane is awaited inline and
    its result is returned unchanged (invariant 1); one shadow lane per
    model in `models` is spawned as an exception-guarded background task
    and its result is discarded (invariant 2).

    Model choice never touches call arguments - each lane's model lives on
    its `Lane` and is read via `current_lane()` downstream (see provider,
    phase 3). This decorator only spawns lanes.

    `sample_rate` gates the whole query: with 0.3, roughly 30% of calls
    spawn shadows at all, and the other 70% run primary-only. Defaults to
    1.0 - every run is shadowed unless a caller opts into sampling to cut
    cost. This is the cost control called for in architecture.md - N+1
    model spend on every request otherwise - but it must be requested, not
    assumed.

    `enabled=False`, or the `AMC_DISABLED` environment variable set to
    anything but an empty/falsy string ("0", "false", "no", "off"), turns
    shadowing off entirely: the primary still runs - still tagged as the
    primary lane, so its real side effects aren't blocked by invariant 4 -
    but no shadow lane is ever spawned.

    `recorder`, if given, must be an already-started `recorder.Recorder`
    (or anything satisfying `RunRecorder`). It captures the query, every
    lane, and every recorded event, and its per-lane observed model is what
    the invariant 6 check (`assert_distinct_from_primary`) runs against
    after each shadow finishes - the check fires automatically on real runs
    when a recorder is present. `on_model_collision` handles a detected
    collision; the default prints and re-raises into the shadow task.
    """
    if not 0.0 <= sample_rate <= 1.0:
        raise ValueError(f"sample_rate must be within [0, 1], got {sample_rate}")

    _rng = rng if rng is not None else random

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            query_id = uuid.uuid4().hex[:12]
            primary_lane = Lane(f"{query_id}-primary", Role.PRIMARY, primary_model)

            will_sample = bool(models) and enabled and not _env_disabled()
            if will_sample:
                will_sample = _rng.random() < sample_rate

            if recorder is not None:
                recorder.start_query(
                    query_id,
                    input=describe_input(args, kwargs),
                    was_sampled=will_sample,
                    primary_lane_id=primary_lane.id,
                )
                recorder.start_lane(primary_lane, query_id=query_id)

            try:
                with lane_scope(primary_lane):
                    result = await fn(*args, **kwargs)
            except Exception as exc:
                if recorder is not None:
                    recorder.finish_lane(
                        primary_lane, status=LaneStatus.ERROR,
                        error_type=type(exc).__name__,
                    )
                raise
            finally:
                discard_overlay(primary_lane.id)   # primary rarely has one; be symmetric
            if recorder is not None:
                recorder.finish_lane(
                    primary_lane, status=LaneStatus.OK,
                    final_output=_stringify(result),
                )

            if will_sample:
                for i, model in enumerate(models):
                    shadow_lane = Lane(f"{query_id}-shadow-{i}", Role.SHADOW, model)
                    if recorder is not None:
                        recorder.start_lane(shadow_lane, query_id=query_id)
                    _spawn_shadow(_ShadowSpawn(
                        fn=fn,
                        lane=shadow_lane,
                        args=args,
                        kwargs=kwargs,
                        on_error=on_shadow_error,
                        on_collision=on_model_collision,
                        recorder=recorder,
                        primary_lane=primary_lane,
                    ))

            return result

        return wrapper

    return decorator
