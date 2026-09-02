# Product

## The question this answers

> Given my actual agent and my actual workload, which model gives me the best
> combination of quality, speed, cost and tool efficiency?

Public benchmarks answer "which model is better in general." That is not the
same question. A model that scores well on a leaderboard may pick the wrong
tool in *your* agent, take twice as many steps, or retry endlessly against
*your* MCP servers.

## How it works, in one paragraph

The developer wraps their agent's entry point. When a real user query arrives,
the primary model serves it as normal. In parallel, the SDK spawns fully
independent instances of the same agent running on other models. Shadows call
read tools normally and take their own paths, but cannot perform real side
effects — no emails sent, no rows written. Every lane's execution is recorded.
Over accumulated production traffic, the developer gets a comparison report
grounded in their own workload rather than a synthetic eval set.

## Why shadow mode rather than an offline benchmark

- **Real query distribution.** No inventing representative inputs.
- **Improves with traffic.** The dataset grows on its own.
- **Answers the deployment question**, not the leaderboard question.

## Who it is for

Developers running agentic systems in production who are choosing between
models, considering an upgrade, or trying to cut cost without losing behaviour.

## Positioning

The one sentence that survives "isn't this just X":

> Everyone compares models. We run them inside *your* agent, on *your* traffic,
> and report how much of the observed difference we can actually attribute to
> the model.

### Landscape

| Tool | What it does | Why we're different |
|---|---|---|
| LangSmith | Traces and displays runs; playground reruns single calls | Hosted, observe-only; cannot spawn parallel agent instances |
| LiteLLM `silent_model` | Mirrors a single completion call to a shadow model | Mirrors one *call*, not the whole agent — shadow never takes its own path |
| DeepEval / Confident AI | Scores agent runs against datasets | Offline, reference-based; not production traffic |
| Netra / LangWatch | A/B comparison, production-trace evaluation | Runs everything live, so environment noise mixes into every comparison |
| agent-shadow-mode | Shadow execution with stubbed tools | Documents the same limitation we address: stubs break agents that branch on tool results |

### Honest risk

Model-comparison tooling exists and some of it is funded. Our bet is that
whole-agent shadowing on live traffic, plus honest fidelity accounting, is
enough of a difference. Test that bet early by showing it to people who build
agents before investing months.

## Non-goals

- **Not an eval framework.** We do not score agents against expected outputs.
- **Not deterministic replay.** LangGraph time travel and Laminar already cover
  cached replay; we do not compete there.
- **Not an observability platform.** We do not replace tracing. Compose with it.
- **Not a router.** We do not serve shadow output to users, ever.

## The framing trap to avoid

The literature on agent evaluation is explicit that reference-based trajectory
comparison is inherently limited, because multiple valid paths typically exist.

So: **the primary's trajectory is not ground truth.** A shadow that diverges is
not wrong — it took a different valid route. Report divergence descriptively
("these models chose different tools at step 3"), never as a score that implies
a ranking we have not earned.

## Scope for v1

Ship: lane spawning, side-effect isolation, model override, event collection,
deterministic metrics, a comparison report.

Defer: hosted dashboard, auth, multi-tenant backend, LLM judgement layer.

Build the library first. Add the hosted layer only if people ask for it.