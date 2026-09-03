from __future__ import annotations

from ..provenance import LatencySource
from .fixtures import Fixture

# Phase 5 build order, step 3: latency replay, strategy (a) ONLY.
#
#   (a) Replay the primary's observed latency for this exact call. Exact,
#       because it's measured.
#
# Strategy (b) log-normal sampling and (c) per-tool median fallback are build
# order step 6 (distribution fitting) and stay unbuilt. This module is the seam
# they'll slot into; today it does one thing.


def replay_latency(fixture: Fixture | None) -> tuple[float | None, LatencySource | None]:
    """The latency to substitute for a virtual call, and its provenance.
    (None, None) when there's no matching observation - we do not guess."""
    if fixture is None or fixture.latency_ms is None:
        return None, None
    return fixture.latency_ms, LatencySource.REPLAYED
