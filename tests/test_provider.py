import asyncio
from types import SimpleNamespace

import pytest

from amc.context import Lane, Role, current_lane, lane_scope
from amc.provider import (
    AnthropicAdapter,
    GeminiAdapter,
    ModelCollisionError,
    OpenAIAdapter,
    Usage,
    assert_distinct_from_primary,
    run_in_thread,
)

# --- fakes, shaped exactly like the SDKs' own hooks (see docs/architecture.md) ---


class FakeOptions:
    def __init__(self, json_data):
        self.json_data = json_data


class FakeStainlessClient:
    """Stands in for openai.OpenAI / anthropic.Anthropic: `_build_request`
    takes an options object whose `.json_data` is a plain mutable dict."""

    def __init__(self):
        self.calls = 0

    def _build_request(self, options, **kwargs):
        self.calls += 1
        return options


class FakeGeminiApiClient:
    """Stands in for google.genai's BaseApiClient: `_build_request` takes a
    separate `path` argument that carries the model for generateContent."""

    def __init__(self):
        self.calls = 0

    def _build_request(self, http_method, path, request_dict, http_options=None):
        self.calls += 1
        return SimpleNamespace(method=http_method, path=path, data=request_dict)


class FakeGeminiClient:
    """Stands in for google.genai.Client: wraps a shared _api_client."""

    def __init__(self, api_client):
        self._api_client = api_client


# --- OpenAI / Anthropic share the same override mechanics ---


@pytest.mark.parametrize("Adapter", [OpenAIAdapter, AnthropicAdapter])
def test_override_model_rewrites_body_key_for_shadow_lane(Adapter):
    client = FakeStainlessClient()
    Adapter().override_model(client)

    with lane_scope(Lane("s1", Role.SHADOW, "shadow-model")):
        opts = client._build_request(FakeOptions({"model": "prod-model", "messages": []}))

    assert opts.json_data["model"] == "shadow-model"


@pytest.mark.parametrize("Adapter", [OpenAIAdapter, AnthropicAdapter])
def test_override_model_leaves_primary_untouched(Adapter):
    client = FakeStainlessClient()
    Adapter().override_model(client)

    with lane_scope(Lane("p0", Role.PRIMARY, None)):
        opts = client._build_request(FakeOptions({"model": "prod-model", "messages": []}))

    assert opts.json_data["model"] == "prod-model"


@pytest.mark.parametrize("Adapter", [OpenAIAdapter, AnthropicAdapter])
def test_override_model_leaves_unset_lane_untouched(Adapter):
    client = FakeStainlessClient()
    Adapter().override_model(client)

    opts = client._build_request(FakeOptions({"model": "prod-model", "messages": []}))

    assert opts.json_data["model"] == "prod-model"


@pytest.mark.parametrize("Adapter", [OpenAIAdapter, AnthropicAdapter])
def test_override_model_is_idempotent(Adapter):
    client = FakeStainlessClient()
    adapter = Adapter()
    adapter.override_model(client)
    adapter.override_model(client)   # second install must not double-wrap

    with lane_scope(Lane("s1", Role.SHADOW, "shadow-model")):
        opts = client._build_request(FakeOptions({"model": "prod-model", "messages": []}))

    assert opts.json_data["model"] == "shadow-model"
    assert client.calls == 1   # one real _build_request call, not nested wrappers


def test_openai_observed_model_and_usage():
    adapter = OpenAIAdapter()
    response = SimpleNamespace(
        model="gpt-4o-mini-2024-07-18",
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=40,
            prompt_tokens_details=SimpleNamespace(cached_tokens=20),
        ),
    )
    assert adapter.observed_model(response) == "gpt-4o-mini-2024-07-18"
    assert adapter.extract_usage(response) == Usage(tokens_in=100, tokens_out=40, cached_tokens=20)


def test_openai_extract_usage_missing_fields_are_none_not_zero():
    adapter = OpenAIAdapter()
    response = SimpleNamespace(model="gpt-4o-mini", usage=None)
    assert adapter.extract_usage(response) == Usage(tokens_in=None, tokens_out=None, cached_tokens=None)


def test_anthropic_observed_model_and_usage():
    adapter = AnthropicAdapter()
    response = SimpleNamespace(
        model="claude-opus-4-1",
        usage=SimpleNamespace(input_tokens=80, output_tokens=30, cache_read_input_tokens=15),
    )
    assert adapter.observed_model(response) == "claude-opus-4-1"
    assert adapter.extract_usage(response) == Usage(tokens_in=80, tokens_out=30, cached_tokens=15)


# --- Gemini: model lives in the URL path, not the body ---


def test_gemini_override_model_rewrites_path_for_shadow_lane():
    api_client = FakeGeminiApiClient()
    GeminiAdapter().override_model(api_client)

    with lane_scope(Lane("s1", Role.SHADOW, "gemini-shadow")):
        req = api_client._build_request(
            "post", "models/gemini-2.0-flash:generateContent", {"contents": []}
        )

    assert req.path == "models/gemini-shadow:generateContent"
    assert req.data == {"contents": []}   # body untouched - override is path-only


def test_gemini_override_model_leaves_primary_and_unset_untouched():
    api_client = FakeGeminiApiClient()
    GeminiAdapter().override_model(api_client)

    with lane_scope(Lane("p0", Role.PRIMARY, None)):
        req = api_client._build_request(
            "post", "models/gemini-2.0-flash:generateContent", {"contents": []}
        )
    assert req.path == "models/gemini-2.0-flash:generateContent"

    req2 = api_client._build_request(
        "post", "models/gemini-2.0-flash:generateContent", {"contents": []}
    )
    assert req2.path == "models/gemini-2.0-flash:generateContent"


def test_gemini_override_model_unwraps_client_to_shared_api_client():
    api_client = FakeGeminiApiClient()
    client = FakeGeminiClient(api_client)
    GeminiAdapter().override_model(client)   # patch via the wrapping Client...

    with lane_scope(Lane("s1", Role.SHADOW, "gemini-shadow")):
        req = api_client._build_request(   # ...call directly on the shared _api_client
            "post", "models/gemini-2.0-flash:generateContent", {"contents": []}
        )

    assert req.path == "models/gemini-shadow:generateContent"


def test_gemini_override_model_ignores_endpoints_without_a_colon_action():
    api_client = FakeGeminiApiClient()
    GeminiAdapter().override_model(api_client)

    with lane_scope(Lane("s1", Role.SHADOW, "gemini-shadow")):
        req = api_client._build_request("get", "models/gemini-2.0-flash", {})

    assert req.path == "models/gemini-2.0-flash"   # no ":action" - left alone


def test_gemini_observed_model_and_usage():
    adapter = GeminiAdapter()
    response = SimpleNamespace(
        model_version="gemini-2.0-flash-001",
        usage_metadata=SimpleNamespace(
            prompt_token_count=50, candidates_token_count=25, cached_content_token_count=10
        ),
    )
    assert adapter.observed_model(response) == "gemini-2.0-flash-001"
    assert adapter.extract_usage(response) == Usage(tokens_in=50, tokens_out=25, cached_tokens=10)


# --- invariant 6: shadow model_observed == primary's -> fail loudly ---


def test_assert_distinct_from_primary_raises_on_collision():
    lane = Lane("s1", Role.SHADOW, "shadow-model")
    with pytest.raises(ModelCollisionError):
        assert_distinct_from_primary(
            primary_observed="gpt-4o", shadow_observed="gpt-4o", shadow_lane=lane
        )


def test_assert_distinct_from_primary_passes_when_different():
    lane = Lane("s1", Role.SHADOW, "shadow-model")
    assert_distinct_from_primary(
        primary_observed="gpt-4o", shadow_observed="gpt-4o-mini", shadow_lane=lane
    )   # must not raise


@pytest.mark.parametrize(
    "primary_observed,shadow_observed",
    [(None, "gpt-4o"), ("gpt-4o", None), (None, None)],
)
def test_assert_distinct_from_primary_skips_when_either_side_unknown(primary_observed, shadow_observed):
    lane = Lane("s1", Role.SHADOW, "shadow-model")
    assert_distinct_from_primary(
        primary_observed=primary_observed, shadow_observed=shadow_observed, shadow_lane=lane
    )   # can't compare unknowns - must not raise


# --- copy_context() for thread-pool paths ---


@pytest.mark.asyncio
async def test_run_in_thread_preserves_lane_context():
    lane = Lane("s1", Role.SHADOW, "shadow-model")
    with lane_scope(lane):
        seen = await run_in_thread(current_lane)

    assert seen is lane


# --- pass condition: three lanes, three different models, on one shared
# client, verified from what each lane actually saw - no cross-lane leakage.


@pytest.mark.asyncio
async def test_three_concurrent_lanes_three_models_no_cross_lane_leakage():
    client = FakeStainlessClient()
    OpenAIAdapter().override_model(client)

    lanes = [
        Lane("primary", Role.PRIMARY, None),   # no override requested
        Lane("shadow-a", Role.SHADOW, "model-a"),
        Lane("shadow-b", Role.SHADOW, "model-b"),
    ]
    seen: dict[str, str] = {}

    async def call(lane, delay):
        with lane_scope(lane):
            await asyncio.sleep(delay)   # force interleaving across lanes
            opts = client._build_request(FakeOptions({"model": "prod-default", "messages": []}))
            seen[lane.id] = opts.json_data["model"]

    await asyncio.gather(
        call(lanes[0], 0.02),
        call(lanes[1], 0.01),
        call(lanes[2], 0.0),
    )

    assert seen == {
        "primary": "prod-default",   # untouched - primary requested no override
        "shadow-a": "model-a",
        "shadow-b": "model-b",
    }
