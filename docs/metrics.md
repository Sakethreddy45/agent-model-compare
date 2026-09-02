# Metrics

Every metric is labelled **algorithmic** (computed from observed data) or
**judged** (LLM opinion). This split is standard practice in trajectory
evaluation and it must be visible in the schema, not only in the UI.

## The framing rule

Reference-based trajectory comparison is inherently limited because multiple
valid paths typically exist. The primary's trajectory is **not** ground truth.

Therefore divergence metrics are **descriptive, never scores**:

- Report: "Model B chose `fetch` where Model A chose `search` at the planning node."
- Never report: "Model B scored 36%."

A percentage implies a ranking we have not earned.

## Algorithmic metrics

### Tool selection
- **Tool set overlap** — Jaccard on the multiset of tools called per lane.
- **Tool sequence divergence point** — the first node where lanes chose
  differently. Research finds ~60% of divergence occurs in the first two steps,
  so surface the early divergence point prominently; it predicts run-level
  inconsistency at low cost.
- **Argument match rate** — for calls to the same tool at the same node, do the
  normalised arguments match? Assert on tool names and argument patterns rather
  than free text: text output rarely matches exactly even between otherwise
  consistent runs.

### Matching modes
Support several, because "did it match" has more than one meaning: strict
(exact order), unordered (same set), subset, superset. For graph-shaped agents,
compare **node visits and transitions** rather than flat sequence positions.

### Efficiency
- **Step count** per lane; flag repeated identical actions as loops. A loop can
  hide inside a correct final answer.
- **Redundant call count** — same tool, same normalised args, twice in one lane.
- **Retry count** and **error rate** per tool.

### Cost and latency
- **Tokens** — exact, from the provider's usage object. Never estimated.
- **Cost** — tokens × price table. Delegate the table to a maintained library
  rather than hand-rolling it; record the price version per run. Account for
  cached input tokens and reasoning tokens separately — a single blended rate
  is wrong on any model with caching.
- **Latency** — p50 and p95, never means alone. Attribute per step so a 20-step
  loop is visible rather than buried in a trace total.
- **Report measured and total-including-substituted separately.** See
  @docs/virtual-tool-layer.md.

### Fidelity (meta-metrics, specific to this tool)
- % of tool calls executed for real vs virtualised
- % of latency substituted
- Whether the lane was contaminated, and at which step

A lane below a fidelity threshold is excluded from headline comparisons.

## Judged metrics

Deferred past v1. When built:

- **Blind the judge.** Never reveal which model produced which output; shuffle
  order. LLM judges show position bias and self-preference bias.
- **Pairwise, not absolute.** "Which is better and why" beats a 1–10 score,
  which drifts and clusters.
- **Validate once.** Hand-label 50 outputs, run the judge on the same 50, and
  publish the agreement rate next to its verdicts. If it is 60%, say so.
- **Report variance.** Run three times on the same pair; if the verdict flips,
  the difference is not real.

## What we deliberately do not measure

- **Task success against a gold answer.** We have no ground truth on production
  traffic.
- **Final end-state verification.** Rigorous benchmarks verify the resulting
  system state, not just the text. We cannot, because shadows do not write.
  This is a real limitation — document it rather than papering over it.

## Report structure

1. Measured facts (algorithmic)
2. Fidelity summary — what was substituted and how much
3. Judged assessments, clearly marked, with agreement rate
4. Optional LLM-written narrative that *interprets* the numbers and never
   produces them