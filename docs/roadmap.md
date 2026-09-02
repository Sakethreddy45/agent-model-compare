# Roadmap

One phase at a time. A phase is done when its pass condition holds, not when
the code looks finished. Do not build ahead.

**Current phase: 3**

---

## Phase 1 — Containment

`context.py`, `policy.py`, `interceptor.py`

Protects invariant 3 only. Not latency, not fixtures, not overlays.

- [x] `Lane` (frozen), `Role` enum, `current_lane()`, `lane_scope()`
- [x] `Isolation` enum (PASSTHROUGH, BLOCK only), `ToolPolicy` with `source`
- [x] `classify()` with precedence: config > MCP annotations > HTTP method > deny
- [x] `report()` printing the classification table with reasons
- [x] `wrap()` handling both sync and async tools
- [x] Synthetic success stub — never raises

**Pass condition:** one primary and two shadow lanes run concurrently against
an agent with `read_docs` (annotated read-only), `send_email` (annotated
destructive) and `mystery_tool` (unclassified). Assert: `send_email` executed
exactly once; `mystery_tool` executed exactly once; `read_docs` executed three
times; four blocked events logged; a call with no lane scope is blocked.

**Design notes:** config may override an annotation but the override is
recorded as `config_override_annotation` and warned at startup — annotations
are hints, not guarantees, and silent overrides of a safety signal are how
accidents happen. Decide deliberately whether `lane_scope` nesting is allowed.

---

## Phase 2 — Lane runner

`runner.py`

- [x] `@shadow` decorator wrapping any agent callable
- [x] Primary awaited inline; shadows as background tasks
- [x] Per-shadow exception guard
- [x] Fractional sampling

**Pass condition:** primary latency with 2 shadows is within a few ms of solo.
A shadow raising mid-run leaves the primary's result and the other shadow
unaffected. Sampling at 0.3 shadows roughly 30% of runs.

---

## Phase 3 — Model override

`provider/` — adapter per provider

- [ ] Adapter interface: `override_model`, `extract_usage`, `observed_model`
- [ ] OpenAI adapter patching `_build_request`
- [ ] Runtime assertion: shadow `model_observed` == primary's → fail loudly
- [ ] `copy_context()` wrapping for any thread-pool path

**Pass condition:** three lanes, three different models on the wire, verified
from the response. Async and streaming paths both correct. Anthropic and Gemini
SDKs may have no `_build_request` equivalent — check before promising
multi-provider support.

---

## Phase 4 — Recorder and store

`recorder.py`, `store.py`

- [ ] Event schema per @docs/architecture.md, including provenance fields
- [ ] SQLite behind a small interface
- [ ] Buffered writes so logging adds no latency to the primary

**Pass condition:** a full run reconstructable from the store alone. Nulls
distinguishable from zeros.

---

## Phase 5 — Virtual tool layer

Per @docs/virtual-tool-layer.md. Build in that doc's order: fixtures →
latency replay → overlay → fidelity accounting.

**Pass condition:** a shadow calls `create_user` then `get_user` and sees its
own user. Lane latency is within a tolerance of the primary's for the same
tool sequence. Fidelity summary renders.

---

## Phase 6 — Analysis

`analysis/` — pure functions over stored events. No network, no LLM.

- [ ] Tool set overlap, divergence point, argument match rate
- [ ] Step counts, loops, retries, error rates
- [ ] Cost via maintained price table; p50/p95 latency
- [ ] Fidelity meta-metrics

**Pass condition:** metrics computed from a fixture database match hand-checked
values.

---

## Phase 7 — Report

- [ ] Deterministic metrics first
- [ ] Fidelity summary
- [ ] Divergence rendered descriptively, never as a score

---

## Deferred

Judgement layer · hosted dashboard · Postgres · OTel export · error injection ·
additional framework adapters

---

## Build log

Record what broke and what surprised you. This is the raw material for the
writeup, and the writeup is worth more than the code.

- Patching `client._client.request` silently did nothing; the OpenAI SDK calls
  `_client.send`. Correct hook is `_build_request`.
- Bare `run_in_executor` loses contextvars silently; `asyncio.to_thread` copies
  context automatically. The leak depends on how the thread was spawned.
- The `@shadow` decorator does not thread a `model` kwarg through the wrapped
  call. Each lane's model lives on the `Lane` object; phase 3's provider
  adapter is expected to read it via `current_lane().model` inside the tool
  boundary rather than the runner passing it explicitly. Keeps the agent
  callable's signature untouched by shadowing.
- `asyncio.create_task` copies the current context at creation, so entering
  `lane_scope` inside the spawned coroutine (not before `create_task`) is
  both correct and sufficient — no `copy_context()` needed for the task-based
  path. That's a phase 3 concern for the thread-pool path only.
- `@shadow`'s `sample_rate` defaults to 1.0, not some cost-saving fraction.
  This tool's entire premise is comparison grounded in the developer's real
  traffic; silently sampling by default would mean early users get partial,
  possibly misleading coverage without ever having asked for the tradeoff.
  Reducing spend via sampling is opt-in, set explicitly by whoever decides
  they want it.
- Added `enabled` and `AMC_DISABLED` as two switches for the same off state
  (checked with OR, either can disable) rather than picking one - a code-level
  default for normal operation, plus an env var so shadowing can be killed in
  a running deployment with no redeploy. Disabling still runs the primary
  under its own `lane_scope`; skipping that too would leave the lane unset
  and trip invariant 4, blocking the primary's own real side effects.