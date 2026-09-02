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
no header or content-length juggling. Verified on async and streaming paths.

> Patching `client._client.request` does **nothing** — the SDK calls
> `_client.send`. A wrong hook fails silently with no error.

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