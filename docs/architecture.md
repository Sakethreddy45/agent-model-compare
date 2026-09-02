# Architecture

Design order: requirements → invariants → failure modes → boundaries →
components → data → code. Components exist because an invariant demands them,
not because they seemed like tidy modules.

## Components

```
   user query
        │
        ▼
  ┌─────────────┐   spawns N+1 independent runs
  │   Runner    │   primary awaited inline, shadows fire-and-forget
  └──────┬──────┘
         │
  ┌──────┴───────────────────────────────┐
  │                                      │
  ▼ PRIMARY lane                         ▼ SHADOW lanes (N)
  own agent instance                     own agent instance each
  own state                              own state
  tools: reads + WRITES real             tools: reads real, writes virtualised
  │                                      │
  └──────────────┬───────────────────────┘
                 ▼
          ┌─────────────┐
          │  Recorder   │  one event per llm/tool call
          └──────┬──────┘
                 ▼
            Store → Analysis → Report
```

| Component | Responsibility | Invariant it protects |
|---|---|---|
| `context` | Lane identity, contextvar scope | 4 (unset means unknown) |
| `policy` | Tool classification, isolation modes | 3 (side-effect safety) |
| `interceptor` | Tool boundary; routes real vs virtual | 3, 5 |
| `provider` | Per-lane model override, usage extraction | 6 (model correctness) |
| `runner` | Lane spawning and isolation | 1, 2 (independence) |
| `recorder` | Event capture with provenance | 8 (mark substitutions) |
| `analysis` | Deterministic metrics only | 7 (no LLM in measurement) |
| `report` | Rendering; judgement layer separate | 7 |

## Dependency direction

`context` → `policy` → `interceptor` → `provider` → `runner` → `recorder` →
`analysis` → `report`

Never import upward. Specifically: `policy` must not import `interceptor`, or
classification cannot be tested without executing tools — and that is the test
you most want.

## Verified mechanisms

These were tested against real libraries, not assumed. Do not redesign them
without re-running the spikes.

**Per-lane model override uses contextvars.** Isolation holds across
`asyncio.gather`, propagates into nested `create_task`, and shows no leakage at
20 concurrent lanes.

**The OpenAI SDK hook is `_build_request`.** Patch `client._build_request` and
rewrite `options.json_data["model"]`. At that point it is still a plain dict —
no header or content-length juggling. Verified on async and streaming paths,
and end-to-end against a real client (`httpx2.MockTransport`, see phase 3
integration tests) covering both request serialization and response
deserialization.

> Patching `client._client.request` does **nothing** — the SDK calls
> `_client.send`. A wrong hook fails silently with no error.

> Like Anthropic, this SDK also vendors httpx as `httpx2`. A custom
> `http_client=` must be an `httpx2.Client`/`httpx2.AsyncClient` — a plain
> `httpx.Client` fails the SDK's own `isinstance` check.

**Anthropic's SDK hook is the same shape as OpenAI's.** Both are
Stainless-generated, and it shows: `client._build_request` is defined once on
a shared `BaseClient` and used by both `Anthropic` and `AsyncAnthropic`, so one
patch covers sync and async. `options.json_data["model"]` is a plain dict key
there too — rewrite it the same way. Dispatch is `self._client.send(request)`,
same trap as OpenAI. Verified on sync, async, and `messages.stream()`.
One wrinkle only: the SDK vendors its own httpx fork under the import name
`httpx2` (to dodge version clashes), so don't assume `isinstance(x,
httpx.Request)` when handling the return value — duck-type or use the SDK's
own re-exports instead.

**Gemini's SDK hook exists but overrides a different thing.** `google-genai`
is hand-built, not Stainless-generated, and `BaseApiClient._build_request(
http_method, path, request_dict, http_options)` is transport-agnostic — it
returns a plain `HttpRequest` dataclass *before* the SDK picks httpx vs. its
optional `aiohttp` fallback for async, so the hook works no matter which
transport ends up handling the call. `Client._api_client` is shared with
`Client.aio`, so one instance-level patch covers sync, async, and both
streaming variants.

> **The model is not in the JSON body for `generateContent`.** It's
> interpolated into the URL path (`models/{model}:generateContent`) before
> `_build_request` even runs; the transient `request_dict["_url"]["model"]`
> that produced it is deleted by `_build_request` itself. Overriding the model
> means rewriting the `path` string argument, not a body dict key — confirmed
> by capturing the real outgoing URL. A generic adapter that only knows how to
> set `body["model"]` silently no-ops here. See roadmap phase 3.

**LangChain's `ChatOpenAI`/`ChatAnthropic` route through the same
`_build_request` hook — reached via a different attribute than the raw SDK.**
`ChatOpenAI.client`/`.async_client` are `Completions`/`AsyncCompletions`
*resource* objects with no `_build_request` — the raw client is
`.root_client` / `.root_async_client`. `ChatAnthropic` exposes it more
directly at `._client` / `._async_client`, but underscore-prefixed: treat it
as internal API that can move without notice. Confirmed by patching each and
intercepting a real `.invoke()`/`.ainvoke()` call before dispatch — both
carried the rewritten model through to the outgoing request body. The
`OpenAIAdapter`/`AnthropicAdapter` interface only ever takes the raw SDK
client, never a framework wrapper; this is documented on each adapter, not
special-cased, since LangChain isn't a dependency of this project and its
internal attribute names are exactly the kind of thing that changes silently
across versions.

**LangGraph works, including parallel fan-out.** Three concurrent lanes through
`ainvoke` isolate correctly inside parallel branches; 15 lanes produced no
contextvar token errors.

**Thread-pool context loss is real and silent.** A bare `run_in_executor` loses
the override and the shadow quietly runs the primary's model.
`asyncio.to_thread` copies context automatically; a raw executor does not. Use
`copy_context()` explicitly everywhere rather than relying on which spawn path
you happened to take.

## The silent-failure class

Every failure mode in this system is silent. A wrong hook, a lost context, an
unwrapped tool — none raise. They just produce a comparison report saying all
models behave identically.

**Therefore:** assert invariants at runtime, not just in tests. If a shadow's
`model_observed` equals the primary's, fail loudly.

## Data model

Three levels: one **query** fans out to N **lanes**; each lane emits many
**events**. Judgements live in their own table so a judge score can never sit
next to a measured number and get averaged with it.

**queries** — `query_id`, `input`, `created_at`, `was_sampled`, `primary_lane_id`

**lanes** — `lane_id`, `query_id`, `role`, `model_requested`, `model_observed`,
`status`, `error_type`, `started_at`, `ended_at`, `final_output`,
`contaminated_at_step`

**events** — `event_id`, `lane_id`, `seq`, `node_name`, `kind`, `name`,
`args_json`, `tokens_in`, `tokens_out`, `cached_tokens`, `latency_ms`,
`latency_source`, `response_source`, `isolation_mode`, `blocked`, `error_type`

**judgments** — `judgment_id`, `query_id`, `lane_a`, `lane_b`, `verdict`,
`judge_model`, `run_index`

### Data model decisions

- **Store raw events, not aggregates.** Six months on you must be able to
  reconstruct why one model looked cheaper.
- **`node_name`, not just `seq`.** Under parallel fan-out, arrival order varies
  for scheduling reasons unrelated to the model. Comparing position 3 against
  position 3 reports noise as divergence. Compare like-named nodes.
- **Store tokens plus a price version; compute cost at read time.** Prices
  change; a stored dollar figure makes old comparisons silently inconsistent.
- **Null ≠ zero.** A missing token count must be distinguishable from zero.
- **Normalise arguments before hashing.** Key order, whitespace and float
  formatting must not read as divergence.

## Cost control

N+1 model spend on every request, continuously. Fractional sampling is an
architectural requirement, not an optimisation — build it into the runner from
the start.

## Storage

SQLite for v1. Postgres later. Write the store behind a small interface so the
swap does not touch the recorder.