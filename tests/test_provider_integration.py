"""Integration tests: each patches a *real* provider SDK client's transport
with `httpx.MockTransport` (or the SDK's vendored `httpx2` fork) and drives a
real call all the way through - real request serialization, real response
deserialization. Unlike tests/test_provider.py's fakes, these catch an
adapter that's wrong about the actual SDK, not just wrong about our own
assumption of its shape.

None of `openai`/`anthropic`/`google-genai`/`httpx2` are project
dependencies - amc's core stays stdlib-only (see CLAUDE.md). Each test
`importorskip`s its own SDK so the suite stays green without them and picks
them up automatically if a developer installs them locally.
"""
import json

import pytest

from amc.context import Lane, Role, lane_scope
from amc.provider import AnthropicAdapter, GeminiAdapter, OpenAIAdapter, Usage


def test_openai_adapter_against_real_sdk():
    httpx2 = pytest.importorskip("httpx2")
    openai = pytest.importorskip("openai")

    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-amc",
                "object": "chat.completion",
                "created": 0,
                "model": "amc-shadow-openai-response",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi there"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                    "total_tokens": 16,
                    "prompt_tokens_details": {"cached_tokens": 2},
                },
            },
        )

    client = openai.OpenAI(
        api_key="sk-test-dummy",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    OpenAIAdapter().override_model(client)

    with lane_scope(Lane("shadow-1", Role.SHADOW, "amc-shadow-openai-request")):
        response = client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )

    # request: override_model actually changed what went out over the wire
    assert captured["body"]["model"] == "amc-shadow-openai-request"

    # response: real SDK deserialization, read back through our adapter
    adapter = OpenAIAdapter()
    assert adapter.observed_model(response) == "amc-shadow-openai-response"
    assert adapter.extract_usage(response) == Usage(tokens_in=12, tokens_out=4, cached_tokens=2)


def test_anthropic_adapter_against_real_sdk():
    httpx2 = pytest.importorskip("httpx2")
    anthropic = pytest.importorskip("anthropic")

    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "id": "msg_amc",
                "type": "message",
                "role": "assistant",
                "model": "amc-shadow-anthropic-response",
                "content": [{"type": "text", "text": "hi there"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 9,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 3,
                },
            },
        )

    client = anthropic.Anthropic(
        api_key="sk-test-dummy",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    AnthropicAdapter().override_model(client)

    with lane_scope(Lane("shadow-1", Role.SHADOW, "amc-shadow-anthropic-request")):
        response = client.messages.create(
            model="claude-opus-4-1",
            max_tokens=8,
            messages=[{"role": "user", "content": "hi"}],
        )

    assert captured["body"]["model"] == "amc-shadow-anthropic-request"

    adapter = AnthropicAdapter()
    assert adapter.observed_model(response) == "amc-shadow-anthropic-response"
    assert adapter.extract_usage(response) == Usage(tokens_in=9, tokens_out=5, cached_tokens=3)


def test_gemini_adapter_against_real_sdk():
    httpx = pytest.importorskip("httpx")
    genai = pytest.importorskip("google.genai")
    from google.genai import types

    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"parts": [{"text": "hi there"}], "role": "model"},
                        "finishReason": "STOP",
                    }
                ],
                "modelVersion": "amc-shadow-gemini-response",
                "usageMetadata": {
                    "promptTokenCount": 5,
                    "candidatesTokenCount": 3,
                    "cachedContentTokenCount": 1,
                },
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = genai.Client(
        api_key="test-dummy", http_options=types.HttpOptions(httpx_client=http_client)
    )
    GeminiAdapter().override_model(client)

    with lane_scope(Lane("shadow-1", Role.SHADOW, "amc-shadow-gemini-request")):
        response = client.models.generate_content(model="gemini-2.0-flash", contents="hi")

    # request: the model is a URL path segment here, not a body key
    assert captured["url"].endswith("models/amc-shadow-gemini-request:generateContent")
    assert "model" not in captured["body"]

    adapter = GeminiAdapter()
    assert adapter.observed_model(response) == "amc-shadow-gemini-response"
    assert adapter.extract_usage(response) == Usage(tokens_in=5, tokens_out=3, cached_tokens=1)
