from __future__ import annotations
import re
from typing import Any

from ..context import current_lane
from .base import Usage

_INSTALLED_ATTR = "_amc_gemini_override_installed"

# Matches the "models/<name>:" segment google-genai builds into the path for
# content-generation calls (generateContent, streamGenerateContent, predict,
# countTokens, ...). Deliberately narrow: endpoints that name a model without
# a trailing ":action" (get/list model) aren't calls we'd want to override.
_MODEL_IN_PATH = re.compile(r"^models/[^:/]+:")


class GeminiAdapter:
    """Duck-typed against `google.genai.Client` / `AsyncClient`, or a bare
    `BaseApiClient` - amc does not import `google.genai`, so the core stays
    stdlib-only. Verified against the installed SDK, sync and async, both
    streaming variants (see docs/architecture.md, "Verified mechanisms").

    Unlike OpenAI/Anthropic: the model is not a body key for `generateContent`
    - it's interpolated into the URL path before `_build_request` runs. This
    adapter rewrites the `path` argument, not `request_dict`. A shared
    "set this dict key" helper across all three adapters would silently no-op
    here - see docs/roadmap.md phase 3 design note.

    `Client._api_client` is shared with `Client.aio`, so one call here covers
    both sync and async.
    """

    def override_model(self, client: Any) -> None:
        api_client = getattr(client, "_api_client", client)
        if getattr(api_client, _INSTALLED_ATTR, False):
            return
        original_build_request = api_client._build_request

        def patched_build_request(
            http_method: str,
            path: str,
            request_dict: dict[str, object],
            http_options: Any = None,
        ) -> Any:
            lane = current_lane()
            if lane is not None and lane.model is not None:
                path = _MODEL_IN_PATH.sub(f"models/{lane.model}:", path)
            return original_build_request(http_method, path, request_dict, http_options)

        api_client._build_request = patched_build_request
        setattr(api_client, _INSTALLED_ATTR, True)

    def observed_model(self, response: Any) -> str | None:
        return getattr(response, "model_version", None)

    def extract_usage(self, response: Any) -> Usage:
        meta = getattr(response, "usage_metadata", None)
        if meta is None:
            return Usage(tokens_in=None, tokens_out=None, cached_tokens=None)
        return Usage(
            tokens_in=getattr(meta, "prompt_token_count", None),
            tokens_out=getattr(meta, "candidates_token_count", None),
            cached_tokens=getattr(meta, "cached_content_token_count", None),
        )
