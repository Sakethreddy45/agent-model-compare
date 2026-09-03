from __future__ import annotations
from enum import Enum

# Closed provenance vocabularies, shared by the store, the interceptor, the
# virtual tool layer and the recorder. Kept in their own leaf module (imports
# nothing) so none of those has to depend on another just to name a source.
#
# Invariant 8: any value not directly measured carries its provenance. These
# enums are how that mark is written; never fall back to a bare string.


class EventKind(str, Enum):
    LLM = "llm"
    TOOL = "tool"


class LatencySource(str, Enum):
    MEASURED = "measured"    # timed directly on this call
    REPLAYED = "replayed"    # the primary's observed latency for the same call
    SAMPLED = "sampled"      # drawn from a fitted per-tool distribution
    MEDIAN = "median"        # per-tool median fallback, primary never made this call


class ResponseSource(str, Enum):
    REAL = "real"            # the tool actually ran
    FIXTURE = "fixture"      # replayed from a response the primary observed
    SCHEMA = "schema"        # synthesised from the tool's declared outputSchema
    TEMPLATE = "template"    # a response template supplied in user config
    STUB = "stub"            # nothing to go on - fixture, schema and template all absent
