from __future__ import annotations
from dataclasses import dataclass
from enum import Enum

# PRECEDENCE (highest wins):
#   1. user config
#   2. MCP annotations (readOnlyHint / destructiveHint)
#   3. transport heuristic (GET safe, others not)
#   4. default DENY
# Config may override an annotation, but the override is recorded and warned.


class Isolation(str, Enum):
    PASSTHROUGH = "passthrough"   # shadows may execute for real
    VIRTUAL = "virtual"          # shadows run against the virtual tool layer
    BLOCK = "block"               # shadows may not run at all

    # PARTITION / DRY_RUN from docs/virtual-tool-layer.md are not built yet.


@dataclass(frozen=True)
class ToolPolicy:
    name: str
    isolation: Isolation
    source: str                   # why it was classified this way


def classify(
    tool_names: list[str],
    config: dict[str, str] | None = None,
    annotations: dict[str, dict] | None = None,
    http_methods: dict[str, str] | None = None,
) -> dict[str, ToolPolicy]:
    config = config or {}
    annotations = annotations or {}
    http_methods = http_methods or {}
    out: dict[str, ToolPolicy] = {}

    for name in tool_names:
        ann = annotations.get(name, {})
        ann_safe = ann.get("readOnlyHint") is True and ann.get("destructiveHint") is not True

        if name in config:
            iso = Isolation(config[name])
            src = "config"
            if ann and iso is Isolation.PASSTHROUGH and not ann_safe:
                src = "config_override_annotation"
                print(f"[amc] WARNING: config marks {name!r} safe but its "
                      f"MCP annotation does not. Config wins.")
        elif ann:
            iso = Isolation.PASSTHROUGH if ann_safe else Isolation.BLOCK
            src = "annotation"
        elif name in http_methods:
            iso = (Isolation.PASSTHROUGH
                   if http_methods[name].upper() in {"GET", "HEAD", "OPTIONS"}
                   else Isolation.BLOCK)
            src = "transport"
        else:
            iso = Isolation.BLOCK
            src = "default_deny"

        out[name] = ToolPolicy(name, iso, src)
    return out


def report(policies: dict[str, ToolPolicy]) -> str:
    rows = [f"{p.name:24} {p.isolation.value:12} ({p.source})"
            for p in sorted(policies.values(), key=lambda p: p.name)]
    return "TOOL CLASSIFICATION\n" + "\n".join(rows)