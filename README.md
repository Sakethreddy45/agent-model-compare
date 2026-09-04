# agent-model-compare

**A model- and framework-agnostic Python SDK for evaluating and selecting LLMs for agentic AI systems.**

Choosing an LLM for an agentic AI system is difficult because traditional benchmarks don't show how models behave on your actual agentic workflows, tools, state, and tasks.

**agent-model-compare** runs the same agentic AI query or task across multiple candidate models in parallel, with each model executing in an isolated evaluation lane. This allows models to independently make decisions, use tools, and diverge without affecting users, production state, or other evaluation lanes.

The SDK captures execution behavior and compares models across:

* **Quality & task success**
* **Latency**
* **Cost & token usage**
* **Tool calls & selection**
* **Execution steps**
* **Retries & errors**
* **State changes**
* **Execution trajectory**

## How It Works

```text
                    Agentic AI Task
                           │
                    agent-model-compare
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
       Model 1          Model 2           Model N
          │                │                │
      Isolated          Isolated         Isolated
        Lane              Lane             Lane
          │                │                │
          └────────────────┼────────────────┘
                           ▼
                    Trace Collection
                           │
                           ▼
                    Model Comparison
                           │
                           ▼
                    Adoption Decision
```

## Key Features

* **Parallel evaluation**: Run multiple candidate models on the same task concurrently.
* **Model & framework agnostic**: Designed to work across LLM providers and agent frameworks.
* **Isolated execution**: Prevent evaluations from affecting production state or other models.
* **Tool safety**: Sandbox, stub, overlay, or block unsafe side effects.
* **Trajectory capture**: Record LLM calls, tool calls, steps, tokens, latency, errors, and state transitions.
* **Data-driven comparison**: Evaluate models across representative agentic AI tasks.
* **Measurement fidelity**: Distinguish real, simulated, stubbed, or substituted execution.

## Goal

Help teams determine **which LLM performs best for their actual agentic AI system and workload** based on quality, reliability, latency, cost, and tool efficiency.
