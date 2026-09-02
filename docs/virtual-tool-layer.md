# The Virtual Tool Layer

How shadow lanes execute side-effecting tools without touching the real world, while preserving both **agent behaviour** and **metric accuracy**.

---

## The problem, stated precisely

A shadow lane must not create the user, send the email, or write the row. But if we simply block the call:

1. **Behaviour breaks.** The agent's next step depends on the result. It reads back what it wrote, branches on a returned ID, or retries because it got an error. We end up measuring our own interference instead of the model.
2. **Metrics break.** A stub returns in 0ms. The real call took 340ms and may have cost money. The lane's totals are now understated by an unknown amount.

Blocking is a mock. What we need is a **virtual service**: something that behaves like the real dependency — same response shape, same state semantics, same timing, same failure rate — without the real consequence.

---

## Design principle

> **Capture from the primary. Replay to the shadows. Record what was substituted.**

The primary lane executes every tool for real. That execution is our source of truth for what a response looks like, how long it takes, and how often it fails. Shadows are served from that observed reality rather than from anything we invent.

Anything we cannot source from observation gets **marked as imputed**, never silently guessed.

---

## Four dimensions of fidelity

A virtual tool call must match the real one on four axes. Each has its own mechanism.

| Dimension | Mechanism | Fidelity |
|---|---|---|
| Response content | Fixture from primary, or schema-generated | high / medium |
| State consistency | Per-lane copy-on-write overlay | high |
| Latency | Log-normal sampled from observed distribution | high |
| Failure modes | Observed error rate replayed stochastically | medium |

---

## 1. Response content

**Primary source — the fixture store.** When the primary executes `create_user(email=x)` and gets back `{"id": 8412, "status": "active"}`, we store that keyed by `(tool_name, normalised_args)`. A shadow making the same call is served that response.

**Secondary source — schema synthesis.** MCP servers expose an `outputSchema` in `tools/list`. When a shadow calls something the primary never called, generate a response conforming to that schema. Lower fidelity, but structurally valid so the agent can parse it.

**Tertiary — declared template.** The user config can supply a response template per tool for cases where neither of the above works.

**Argument normalisation matters.** `{"a":1,"b":2}` and `{"b":2,"a":1}` are the same call. Normalise key order, whitespace, and float formatting before hashing, or identical calls will read as cache misses.

**Identifier collision.** If the fixture returns `id: 8412` and the shadow later writes a *different* record, both map to the same ID. Allocate shadow-scoped synthetic IDs in a reserved range and maintain a per-lane mapping, so the agent's internal references stay coherent.

---

## 2. State consistency — the copy-on-write overlay

This is what makes the "next step breaks" problem go away.

```
        shadow lane's view
   ┌──────────────────────────────┐
   │  Delta layer (per-lane dict) │  ← writes land here
   ├──────────────────────────────┤
   │  Real state (read-only)      │  ← reads fall through
   └──────────────────────────────┘
```

**Read:** check the lane's delta first; if absent, read real state.
**Write:** record in the delta only. Never touch real state.
**Teardown:** discard the delta when the lane completes.

So a shadow that calls `create_user` then `get_user` sees its own user. The agent proceeds exactly as it would have. This is the same mechanism Turso's AgentFS uses for filesystems — reads pass through to the base layer, writes are redirected to a separate store, the original is never modified.

**Where it works:** any tool whose state you can intercept — your own DB, your own MCP servers, in-process caches.

**Where it doesn't:** third-party APIs you don't control. Also note the standard caveat for virtual filesystem layers — isolation is logical, not physical. If a tool shells out to a subprocess, that subprocess sees real state unless you wrap exec too.

---

## 3. Latency — the part everyone gets wrong

A stub returning instantly makes the shadow look artificially fast. Three strategies, in preference order.

**a) Replay the primary's observed latency.** The primary made the same call and it took 340ms. Sleep 340ms in the shadow. Exact, because it's measured.

**b) Sample from a fitted distribution.** For tools called many times, fit a **log-normal** distribution over observed latencies and sample. This is what Hoverfly does, and log-normal is the right family because API latency is right-skewed — a mean alone would systematically understate tail behaviour.

**c) Per-tool median fallback.** For a tool the primary never called, use the median across all observations of that tool. Mark it imputed.

**Async matters.** Use `await asyncio.sleep(d)`, not `time.sleep(d)`, or you block the event loop and distort every other lane's measurements.

**A subtlety worth handling:** latency should be sampled *per call*, not fixed per tool. If you replay a constant 340ms, the shadow's variance is zero and any variance metric you compute downstream is meaningless.

---

## 4. Failure modes

Real tools fail. A virtual tool that always succeeds gives the shadow an easier run than the primary had, and hides whether the model handles errors well — which is one of the more interesting things to compare.

**Mechanism:** track the observed error rate per tool from primary traffic. When a shadow calls it, sample against that rate; on a hit, return the recorded error response with its recorded latency (errors often have very different timing than successes — timeouts especially).

**Determinism:** seed the sampler per `(lane_id, tool, seq)` so a lane's failure pattern is reproducible when re-run.

**Opt-in.** Default this off for v1. It adds variance that makes early comparisons noisier, and it's only worth enabling once the basics are stable.

---

## Architecture

```
   agent tool call
         │
         ▼
   ┌──────────────────────┐
   │  Tool Interceptor    │  reads lane contextvar
   └──────────┬───────────┘
              │
      primary ├──────────────► real tool ──► observe & record
              │                                  │
              │                                  ▼
              │                          ┌───────────────┐
              │                          │ Fixture Store │
              │                          │ latency dist  │
              │                          │ error rates   │
              │                          └───────┬───────┘
              │                                  │
      shadow  └──────────────► Virtual Tool ◄────┘
                                    │
                                    ├── resolve response (fixture → schema → template)
                                    ├── apply overlay read/write
                                    ├── sleep sampled latency
                                    └── emit event {imputed: true, fidelity: "fixture"}
```

**Six components:**

1. **Interceptor** — wraps the tool boundary, reads the lane contextvar, routes to real or virtual.
2. **Classifier** — decides each tool's isolation mode at startup from MCP annotations (`readOnlyHint`, `destructiveHint`), HTTP method, then user config. Default deny.
3. **Fixture store** — `(tool, normalised_args) → {response, latency_ms, error, observed_at}`.
4. **Overlay** — per-lane copy-on-write state.
5. **Latency model** — fitted distributions per tool.
6. **Recorder** — emits an event per call with the fidelity level used.

---

## Isolation modes

Not a boolean. A per-tool setting:

| Mode | Behaviour | Use when |
|---|---|---|
| `passthrough` | Execute for real | `readOnlyHint: true` |
| `overlay` | Real logic, writes to lane delta | You control the state store |
| `partition` | Real execution, shadow tenant/namespace | DB supports tenant scoping |
| `dry_run` | Tool's own dry-run flag | Tool honours `X-Dry-Run` / `Prefer: return=minimal` |
| `virtual` | Fixture + overlay + latency | Default for destructive tools |
| `block` | Hard refuse, log, mark lane contaminated | Payments, anything irreversible |

Defaults come from MCP annotations. The spec is explicit these are **hints, not guarantees** — a server can mislabel a destructive tool — so annotations set the default and user config is authoritative.

---

## Metric accounting

Every event carries provenance:

```
latency_ms          340
latency_source      "measured" | "replayed" | "sampled" | "median"
response_source     "real" | "fixture" | "schema" | "template"
isolation_mode      "passthrough" | "overlay" | "virtual" | ...
```

The report then shows **two latency figures per lane**: measured-only, and total-including-substituted. Never one blended number.

**What stays exact regardless:** every model call is real, so tokens, model cost, and inference latency are measured for all lanes. Since model calls usually dominate both cost and wall time, the substituted portion is a minority of the total — but the report should state what percentage it was, not assume it's negligible.

---

## Fidelity score

Each lane gets a fidelity summary:

```
lane: shadow1 (claude-sonnet-4-6)
  tool calls:            14
  executed for real:      9  (64%)
  served from fixture:    4  (29%)
  schema-synthesised:     1  ( 7%)
  latency substituted:  412ms of 3,180ms (13%)
  contaminated:          no
```

A lane whose fidelity drops below a threshold gets flagged and excluded from headline comparisons. This is the honest alternative to pretending substitution didn't happen.

---

## When a shadow calls something the primary never did

The common and unavoidable case, since divergent models take divergent paths.

1. No fixture exists → synthesise from `outputSchema`.
2. No latency observation → use per-tool median, mark `sampled`.
3. Mark the event `response_source: "schema"`.
4. If this happens on a *destructive* tool, the lane's state diverges from anything grounded in reality — increment a divergence counter.
5. Past a configurable threshold, mark the lane `contaminated` and drop it from trajectory comparison beyond that step.

Partial data is still useful. A lane contaminated at step 9 has nine valid steps.

---

## Build order

1. Interceptor + classifier + hard `block` mode. Prove nothing escapes.
2. Fixture store + `virtual` mode with schema fallback.
3. Latency replay (strategy a only).
4. Overlay for stateful tools.
5. Fidelity accounting and reporting.
6. Distribution fitting, then error injection — last, and opt-in.

---

## Honest limits

- Logical isolation, not physical. Subprocesses and unwrapped clients bypass the overlay.
- Fixtures go stale. A response recorded Monday may not reflect Tuesday's reality.
- Fidelity degrades as writes move earlier in the trajectory. Document this: the tool works best on read-heavy agents.
- Schema-synthesised responses are structurally valid but semantically empty. An agent that reasons over the *content* of a write response will behave differently.
- Error injection changes what you're measuring. Off by default for a reason.