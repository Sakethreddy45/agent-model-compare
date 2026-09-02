from __future__ import annotations
from typing import Any

from ..context import current_lane
from .base import Usage

_INSTALLED_ATTR = "_amc_anthropic_override_installed"


class AnthropicAdapter:
    """Duck-typed against `anthropic.Anthropic` / `anthropic.AsyncAnthropic`
    - amc does not import `anthropic`, so the core stays stdlib-only. Same
    hook shape as OpenAI's (both are Stainless-generated); verified against
    the installed SDK, including `messages.stream()` (see docs/
    architecture.md, "Verified mechanisms").

    Pass the raw SDK client to `override_model`. For `langchain_anthropic.
    ChatAnthropic`, that's `chat_model._client` (sync) or
    `chat_model._async_client` (async) - underscore-prefixed, so treat it as
    fragile internal API that may move without notice.

    Note: this SDK vendors its own httpx fork under the import name
    `httpx2`; don't `isinstance`-check the built request against `httpx.
    Request`.
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
        return Usage(
            tokens_in=getattr(usage, "input_tokens", None),
            tokens_out=getattr(usage, "output_tokens", None),
            cached_tokens=getattr(usage, "cache_read_input_tokens", None),
        )
