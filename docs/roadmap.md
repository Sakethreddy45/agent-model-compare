# Roadmap

One phase at a time. A phase is done when its pass condition holds, not when
the code looks finished. Do not build ahead.

**Current phase: 4**

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

- [x] Adapter interface: `override_model`, `extract_usage`, `observed_model`
- [x] OpenAI adapter patching `_build_request` — plus Anthropic and Gemini
      adapters, using the shapes confirmed by the phase 3 probe; the
      "adapter per provider" heading and the pass condition below ("three
      different models on the wire") both point at more than one provider.
- [x] Runtime assertion: shadow `model_observed` == primary's → fail loudly
- [x] `copy_context()` wrapping for any thread-pool path
- [x] Probe whether LangChain's `ChatOpenAI` and `ChatAnthropic` route through
      the same `_build_request` hook as the raw SDKs, or wrap/bypass it —
      LangChain's own client construction may not expose the underlying SDK
      client the same way, and that's exactly the kind of layer a hook can
      silently miss.

**Pass condition:** three lanes, three different models on the wire, verified
from the response. Async and streaming paths both correct. Anthropic's SDK
hook is the same shape as OpenAI's (`_build_request`, `options.json_data`) —
confirmed by probe. Gemini's is not: `_build_request` exists but the model
lives in the URL path, not the body, for `generateContent`. See
@docs/architecture.md, "Verified mechanisms". Met two ways: the earlier phase
3 probe scripts drove the real installed SDKs end to end and captured the
outgoing request; `tests/test_provider.py` reproduces the same mechanism
against fakes shaped exactly like those SDKs' hooks (core stays stdlib-only,
so the shipped suite can't import `openai`/`anthropic`/`google-genai`
itself), including three concurrent lanes on one shared client with no
cross-lane leakage.

**Design note:** `override_model` must be implemented per provider, not
factored into one shared "set this dict key" helper. OpenAI and Anthropic
override a body key; Gemini overrides a URL path segment. A shared dict-key
helper would silently no-op on Gemini — the exact silent-failure class this
project exists to catch, now happening inside its own adapter.

**Scope note:** the invariant 6 assertion (`assert_distinct_from_primary`)
ships as a standalone, tested function operating on already-extracted
`model_observed` values — it is not wired into `runner.py` to fire
automatically on every real run. That wiring belongs to phase 4, once the
recorder exists to capture each lane's observed model in the first place;
provider stays usable and testable without it, per the "provider must be
testable without executing" convention in CLAUDE.md.

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
- `override_model` ended up meaning "install a lane-aware hook," not "pin
  this client to one model." A client patched once reads `current_lane()`
  fresh on every call, so the same client instance is safe to share across
  concurrently running lanes - each call resolves its own model independent
  of what any other lane is doing at the same moment. Pinning the client
  per lane would have needed a separate client instance per lane (or the
  last `override_model` call would silently win for every lane sharing it);
  the contextvar approach is what "no leakage at 20 concurrent lanes" in
  architecture.md was actually validating.
- All three adapters are duck-typed against the real SDKs' attribute names,
  confirmed via probe scripts, but `amc` never imports `openai`, `anthropic`,
  or `google-genai` - those packages only ever existed in a scratch venv
  outside the tracked project, not in `pyproject.toml`. Tests use fakes
  shaped identically to the real hooks instead, so the suite stays
  dependency-free while still exercising the exact mechanism.
- LangChain's `ChatOpenAI`/`ChatAnthropic` reach the raw SDK client under a
  different attribute than expected (`.root_client`, not `.client`; `._client`
  on `ChatAnthropic`, underscore-prefixed). Confirmed by intercepting a real
  `.invoke()`. Documented on each adapter rather than special-cased in code -
  no LangChain import in `amc`, and those attribute names are exactly the
  kind of internal surface that moves without a deprecation notice.