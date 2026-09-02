from __future__ import annotations
import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum


class Role(str, Enum):
    PRIMARY = "primary"
    SHADOW = "shadow"


@dataclass(frozen=True)
class Lane:
    id: str
    role: Role
    model: str | None = None

    @property
    def is_primary(self) -> bool:
        return self.role is Role.PRIMARY


_current: contextvars.ContextVar[Lane | None] = contextvars.ContextVar(
    "amc_current_lane", default=None
)


def current_lane() -> Lane | None:
    """None means UNKNOWN, never 'primary'. Callers must handle it."""
    return _current.get()


@contextmanager
def lane_scope(lane: Lane):
    token = _current.set(lane)          # token stays local to this scope
    try:
        yield lane
    finally:
        _current.reset(token)