from __future__ import annotations
from typing import Any

# Secondary response source: when a shadow calls something the primary never
# did, there is no fixture. MCP servers publish an `outputSchema` in
# tools/list; synthesise a response that conforms to it. Structurally valid so
# the agent can parse it, semantically empty - an agent that reasons over the
# *content* of the response will behave differently, and that is a documented
# limit of this layer.


def synthesize(schema: dict | None) -> Any:
    if not isinstance(schema, dict):
        return None

    if schema.get("enum"):
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    if "default" in schema:
        return schema["default"]

    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)

    if t == "object" or (t is None and "properties" in schema):
        props: dict[str, Any] = schema.get("properties", {})
        return {name: synthesize(sub) for name, sub in props.items()}
    if t == "array":
        items = schema.get("items")
        return [synthesize(items)] if isinstance(items, dict) else []
    if t == "string":
        return ""
    if t == "integer":
        return 0
    if t == "number":
        return 0.0
    if t == "boolean":
        return False
    return None
