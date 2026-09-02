from __future__ import annotations
import asyncio
import functools
import os
import random
import uuid
from typing import Any, Awaitable, Callable, Sequence

from .context import Lane, Role, lane_scope

ShadowErrorHandler = Callable[[Lane, Exception], Any]

# Keep strong refs so fire-and-forget tasks aren't GC'd mid-flight
# (asyncio only holds a weak reference once nothing else does).
_background_tasks: set[asyncio.Task] = set()

_ENV_DISABLE_VAR = "AMC_DISABLED"
_ENV_FALSY = {"", "0", "false", "no", "off"}


def _env_disabled() -> bool:
    # Read fresh on every call, not once at decoration time, so ops can flip
    # this in a running process (or a test can monkeypatch it) with no redeploy.
    return os.environ.get(_ENV_DISABLE_VAR, "").strip().lower() not in _ENV_FALSY


def _default_on_shadow_error(lane: Lane, exc: Exception) -> None:
    print(f"[amc] shadow lane {lane.id} ({lane.model}) raised "
          f"{type(exc).__name__}: {exc}")


async def _run_shadow(
    fn: Callable[..., Awaitable[Any]],
    lane: Lane,
    args: tuple,
    kwargs: dict,
    on_error: ShadowErrorHandler,
) -> None:
    """Isolated per invariant 2: a raise here must reach no one but this
    lane's own error handler. Primary and sibling shadows are untouched."""
    try:
        with lane_scope(lane):
            await fn(*args, **kwargs)
    except Exception as exc:      # noqa: BLE001 - deliberate shadow guard
        on_error(lane, exc)


def _spawn_shadow(
    fn: Callable[..., Awaitable[Any]],
    lane: Lane,
    args: tuple,
    kwargs: dict,
    on_error: ShadowErrorHandler,
) -> asyncio.Task:
    # lane_scope is entered inside _run_shadow, i.e. inside the task's own
    # coroutine, never here - contextvars must be set after spawning.
    task = asyncio.create_task(_run_shadow(fn, lane, args, kwargs, on_error))
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
    rng: random.Random | None = None,
    enabled: bool = True,
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
    but no shadow lane is ever spawned. This is what lets `@shadow` stay on
    a production entry point permanently, toggled by deploy config rather
    than a code change.
    """
    if not 0.0 <= sample_rate <= 1.0:
        raise ValueError(f"sample_rate must be within [0, 1], got {sample_rate}")

    _rng = rng if rng is not None else random

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            query_id = uuid.uuid4().hex[:12]

            primary_lane = Lane(f"{query_id}-primary", Role.PRIMARY, primary_model)
            with lane_scope(primary_lane):
                result = await fn(*args, **kwargs)

            if enabled and not _env_disabled() and models and _rng.random() < sample_rate:
                for i, model in enumerate(models):
                    shadow_lane = Lane(f"{query_id}-shadow-{i}", Role.SHADOW, model)
                    _spawn_shadow(fn, shadow_lane, args, kwargs, on_shadow_error)

            return result

        return wrapper

    return decorator
