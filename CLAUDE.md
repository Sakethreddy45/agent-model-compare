# agent-model-compare

Shadow-testing SDK for LLM agents. Runs the same production query across
multiple models in parallel background lanes to compare tool selection, step
count, cost and latency — without affecting the user's response.

Package: `amc`  ·  Python 3.11+  ·  stdlib-only core

## Reference docs

- @docs/product.md — what this is, who it's for, what it is not
- @docs/architecture.md — components, boundaries, data model
- @docs/virtual-tool-layer.md — side-effect isolation and metric fidelity
- @docs/metrics.md — metric definitions and how they're computed
- @docs/roadmap.md — build phases, current status, pass conditions

Read the phase entry in @docs/roadmap.md before starting work.

## Invariants — never violate these

1. **Primary independence.** Shadow execution must never change the primary
   lane's response or add latency to it. Shadows are fire-and-forget.
2. **Shadow independence.** One shadow's failure must not affect any other
   lane. Every shadow task is individually exception-guarded.
3. **Side-effect safety.** A shadow may never perform an unapproved real-world
   side effect. Unclassified tools are DENIED, not allowed.
4. **Unset means unknown.** If the lane contextvar is unset, treat it as
   unknown and refuse destructive tools. Never infer "primary" from absence.
5. **Blocked tools return success.** A blocked tool returns a synthetic success
   response, never an exception. An exception makes the agent branch
   differently and we would be measuring our own interference.
6. **Model correctness.** Record both `model_requested` and `model_observed`.
   If a shadow's observed model equals the primary's, the lane is INVALID —
   fail loudly. This is the silent failure that invalidates everything.
7. **No LLM in the measurement path.** Deterministic metrics are computed from
   observed data. LLM judgement is a separate, clearly-labelled layer.
8. **Mark every substitution.** Any value not directly measured carries its
   provenance (`measured` / `replayed` / `sampled` / `median`). Never blend
   measured and imputed numbers into one figure.

## Conventions

- Modules depend downward only: `context` → `policy` → `interceptor` →
  `recorder` → `analysis`. Never import upward. `policy` must be testable
  without executing any tool.
- Dataclasses for records; `frozen=True` for anything identity-like.
- Enums for closed sets (Role, Isolation, LatencySource). No bare strings.
- Type hints on all public functions.
- Async-first. Use `await asyncio.sleep()`, never `time.sleep()` — blocking the
  loop distorts every other lane's measurements.
- Set contextvars *inside* the task or scope, never before spawning. Keep the
  reset token local to its scope.
- Wrap thread-pool work with `contextvars.copy_context()`. Context loss across
  threads is silent and produces wrong data with no error.

## Testing

- `pytest`, asyncio_mode = auto. Every phase needs a passing test before the
  next phase starts.
- Containment tests are non-negotiable: assert real side effects happened
  exactly once, from the primary only.
- Prefer fakes over mocks for tools. Assert on recorded events, not internals.

## Do not

- Do not add dependencies without asking. The core is stdlib-only.
- Do not build ahead of the current phase in @docs/roadmap.md.
- Do not add an LLM call anywhere outside the judgement layer.
- Do not write excessive comments. Comment *why*, never *what*.
- Do not silently catch exceptions in the primary lane.