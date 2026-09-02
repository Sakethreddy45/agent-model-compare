from __future__ import annotations
import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Protocol, TypeVar

from ..context import Lane

_T = TypeVar("_T")


@dataclass(frozen=True)
class Usage:
    tokens_in: int | None
    tokens_out: int | None
    cached_tokens: int | None = None


class ModelAdapter(Protocol):
    """One adapter per provider - see docs/roadmap.md phase 3 design note.
    Each provider overrides the model differently (OpenAI/Anthropic rewrite a
    body key, Gemini rewrites a URL path segment); there is deliberately no
    shared "set this dict key" implementation behind this interface, since
    that would silently no-op on Gemini.

    `client` is always the raw provider SDK client (e.g. `openai.OpenAI`),
    never an agent-framework wrapper - see docs/architecture.md, "Verified
    mechanisms", for where to find the raw client inside LangChain's
    ChatOpenAI/ChatAnthropic.
    """

    def override_model(self, client: Any) -> None:
        """Idempotently patch `client` so every call it makes is routed to
        whichever model `current_lane()` names at call time, read fresh per
        call - not a one-shot override to a fixed model. This is what makes
        it safe to call once on a client shared across concurrently running
        lanes (verified: no leakage at 20 concurrent lanes, see
        architecture.md). A call with no lane in scope, or a lane whose
        `model` is None, passes through unmodified."""
        ...

    def observed_model(self, response: Any) -> str | None:
        """The model the provider says it actually used, read from the
        response. Compare against the lane's requested model (invariant 6)."""
        ...

    def extract_usage(self, response: Any) -> Usage:
        """Token counts from the response's usage object. None, not 0, for
        anything the response doesn't report."""
        ...


class ModelCollisionError(RuntimeError):
    """A shadow's observed model matched the primary's (invariant 6). The
    override silently failed - this lane's comparison is meaningless, not
    just imprecise. Never swallow this the way an ordinary tool exception
    is swallowed; it must reach someone."""


def assert_distinct_from_primary(
    *,
    primary_observed: str | None,
    shadow_observed: str | None,
    shadow_lane: Lane,
) -> None:
    """The invariant 6 runtime check. Silent by design when either side is
    unknown - unset means unknown (invariant 4), not "assume collision"."""
    if primary_observed is None or shadow_observed is None:
        return
    if shadow_observed == primary_observed:
        raise ModelCollisionError(
            f"shadow lane {shadow_lane.id!r} (requested {shadow_lane.model!r}) "
            f"observed model {shadow_observed!r}, identical to the primary's. "
            "The override did not take effect; this lane is invalid."
        )


async def run_in_thread(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Run a blocking (sync-only) provider call off the event loop without
    losing the lane contextvar. `asyncio.to_thread` copies the current
    context into the thread automatically; a bare `loop.run_in_executor`
    does not, and the loss is silent - the shadow would quietly run under
    the primary's model with no error. Prefer this over reaching for
    `run_in_executor` directly."""
    return await asyncio.to_thread(fn, *args, **kwargs)
