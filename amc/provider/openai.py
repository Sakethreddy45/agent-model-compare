from __future__ import annotations
from typing import Any

from ..context import current_lane
from .base import Usage

_INSTALLED_ATTR = "_amc_openai_override_installed"


class OpenAIAdapter:
    """Duck-typed against `openai.OpenAI` / `openai.AsyncOpenAI` - amc does
    not import `openai`, so the core stays stdlib-only. Verified against the
    installed SDK (see docs/architecture.md, "Verified mechanisms").

    Pass the raw SDK client to `override_model`. For `langchain_openai.
    ChatOpenAI`, that's `chat_model.root_client` (sync) or
    `chat_model.root_async_client` (async) - `.client`/`.async_client` are
    resource sub-objects and do not have `_build_request`.
    """

    def override_model(self, client: Any) -> None:
        if getattr(client, _INSTALLED_ATTR, False):
            return
        original_build_request = client._build_request

        def patched_build_request(options: Any, *args: Any, **kwargs: Any) -> Any:
            lane = current_lane()
            if lane is not None and lane.model is not None:
                json_data = getattr(options, "json_data", None)
                if isinstance(json_data, dict):
                    json_data["model"] = lane.model
            return original_build_request(options, *args, **kwargs)

        client._build_request = patched_build_request
        setattr(client, _INSTALLED_ATTR, True)

    def observed_model(self, response: Any) -> str | None:
        return getattr(response, "model", None)

    def extract_usage(self, response: Any) -> Usage:
        usage = getattr(response, "usage", None)
        if usage is None:
            return Usage(tokens_in=None, tokens_out=None, cached_tokens=None)
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", None) if details is not None else None
        return Usage(
            tokens_in=getattr(usage, "prompt_tokens", None),
            tokens_out=getattr(usage, "completion_tokens", None),
            cached_tokens=cached,
        )
