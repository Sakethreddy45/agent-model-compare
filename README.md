# agent-model-compare

A model- and framework-agnostic Python SDK for evaluating LLMs in agentic AI systems.

The SDK runs the same agent task across multiple candidate models in parallel using separate execution lanes. Each lane maintains its own state and execution history, allowing the models to make independent decisions about tool use, execution steps, retries, and state changes.

The SDK records and compares:

* Task quality
* Task success
* Latency
* Token usage
* Cost
* Tool calls and tool selection
* Execution steps
* Retries and errors
* State changes
* Execution traces

## Architecture

```text
                         ┌─────────────────┐
                         │   User Query    │
                         └────────┬────────┘
                                  │
                   ┌──────────────▼──────────────┐
                   │       @shadow wrapper       │
                   │    creates evaluation lanes │
                   └──────────────┬──────────────┘
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        │                         │                         │
        ▼                         ▼                         ▼
┌───────────────┐         ┌───────────────┐         ┌───────────────┐
│ PRIMARY LANE  │         │ SHADOW LANE 1 │         │ SHADOW LANE N │
│               │         │               │         │               │
│ inline        │         │ background    │         │ background    │
│ own state     │         │ own state     │         │ own state     │
└───────┬───────┘         └───────┬───────┘         └───────┬───────┘
        │                         │                         │
        └─────────────────────────┼─────────────────────────┘
                                  ▼
                  ┌──────────────────────────────────┐
                  │       PROVIDER ADAPTERS          │
                  │                                  │
                  │ OpenAI │ Anthropic │ Gemini      │
                  └────────────────┬─────────────────┘
                                   │
                                   ▼
                  ┌──────────────────────────────────┐
                  │        TOOL INTERCEPTOR           │
                  │                                  │
                  │ classify and control tool calls  │
                  └───────────────┬──────────────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    ▼                           ▼
             ┌──────────────┐          ┌─────────────────┐
             │ Tool calls   │          │ Isolated tools  │
             │              │          │                 │
             │ execute      │          │ overlay         │
             │ normally     │          │ fixtures        │
             │              │          │ latency         │
             └──────┬───────┘          │ fallback        │
                    │                  │ blocked         │
                    │                  └────────┬────────┘
                    └─────────────┬────────────┘
                                  ▼
                  ┌──────────────────────────────────┐
                  │            RECORDER              │
                  │                                  │
                  │ model calls · tools · tokens    │
                  │ latency · cost · errors · state │
                  └───────────────┬──────────────────┘
                                  ▼
                  ┌──────────────────────────────────┐
                  │             STORE                │
                  │                                  │
                  │ queries → lanes → events        │
                  └───────────────┬──────────────────┘
                                  ▼
                  ┌──────────────────────────────────┐
                  │            ANALYSIS              │
                  │                                  │
                  │ quality · cost · latency         │
                  │ tool usage · execution steps     │
                  │ execution differences            │
                  └───────────────┬──────────────────┘
                                  ▼
                  ┌──────────────────────────────────┐
                  │             REPORT               │
                  │                                  │
                  │ comparison results                │
                  │ execution details                │
                  └──────────────────────────────────┘
```

## Evaluation Lanes

The primary lane runs the agent normally and returns its result.

Shadow lanes run the same task in parallel using different candidate models. Each lane has its own state and execution history.

The models can therefore make independent decisions about:

* Which tools to call
* Which arguments to use
* How many steps to take
* Whether to retry
* How to handle tool errors
* What state changes to make

The primary lane does not depend on the shadow lanes.

## Provider Adapters

The provider adapter layer provides a common interface for different LLM providers.

Supported providers include:

* OpenAI
* Anthropic
* Gemini

The adapters handle model selection and usage information such as input tokens, output tokens, and model name.

## Tool Interception

Tool calls pass through the tool interceptor before execution.

The interceptor determines how a tool should be handled based on available tool information and configured rules.

A shadow lane can:

* Execute a tool
* Use an isolated overlay
* Use a stored fixture
* Add simulated latency
* Generate a fallback response
* Block the call

This prevents evaluation lanes from making unwanted changes to shared application state.

## Trajectory Recording

The recorder stores events from each execution lane.

Recorded information includes:

* Model calls
* Model name
* Input and output tokens
* Cost
* Latency
* Tool calls
* Tool arguments
* Tool results
* Execution steps
* Retries
* Errors
* State changes

Events are buffered before being written to storage so recording does not block agent execution.

## Storage

The storage layer uses an interface so the rest of the SDK does not depend on a specific database.

Execution data is organized as:

```text
Queries
   │
   └── Lanes
          │
          └── Events
```

SQLite is used for local storage, with PostgreSQL available as a storage backend.

## Analysis

The analysis layer compares the recorded executions across models.

Metrics include:

* Task quality
* Task success
* Cost
* Latency
* Token usage
* Tool usage
* Number of execution steps
* Retries and errors
* State changes
* Execution differences

The comparison is based on recorded execution data.

## Report

The report summarizes the differences between candidate models, including:

* Task results
* Cost and token usage
* Latency
* Tool usage
* Execution steps
* Errors and retries
* Differences in execution paths

## Goal

Evaluate multiple LLMs on the same agent workflow by running them in parallel, then compare their task results, execution, cost, latency, tool usage, and errors.
