# Workflow Engine: current implementation contract

This document records the implemented Workflow milestone. It is deliberately narrower than the full product roadmap; unsupported node types are rejected at publish time.

## Definition format

A draft is a JSON object containing `nodes` and `edges`.

```json
{
  "nodes": [
    {"id": "start", "type": "start", "config": {}},
    {
      "id": "answer",
      "type": "llm",
      "config": {
        "model_config_id": "00000000-0000-0000-0000-000000000001",
        "prompt_template": "Answer this question: {{input}}",
        "system_prompt": "You are a helpful assistant.",
        "max_tokens": 512,
        "timeout_seconds": 60
      }
    },
    {"id": "end", "type": "end", "config": {"source_node": "answer"}}
  ],
  "edges": [
    {"source": "start", "target": "answer"},
    {"source": "answer", "target": "end"}
  ]
}
```

The model UUID is illustrative. A real enabled chat model belonging to the same tenant (or an explicitly shared provider) must be selected in the UI/API.

## Supported nodes

| Node | Configuration | Behavior |
|---|---|---|
| `start` | `{}` | Emits the run input object |
| `llm` | `model_config_id`, `prompt_template`, optional `system_prompt`, `max_tokens`, `timeout_seconds`, `input_from` | Calls the configured model through ModelGateway |
| `condition` | `equals`, optional `input_key` or `source_node` | Compares one input/state value and emits `matched` |
| `end` | optional `source_node` | Returns the referenced node output, or the accumulated state |

Edges from a condition node must declare boolean `when` values and include both `true` and `false` branches. The first implementation treats an incoming active predecessor as sufficient to activate a downstream node; it does not yet provide a configurable AND-join.

## Publish validation

The server checks:
- 2–50 nodes and no more than 200 edges;
- stable unique node IDs and supported node types;
- one start node, at least one end node, no self/duplicate edges, no cycles and no unreachable nodes;
- valid condition branches and source references that point to earlier nodes;
- valid UUID model references, enabled chat model, tenant ownership, token ceiling and timeout range;
- no credential-like keys in node configuration.

Publishing stores an immutable `WorkflowVersion.definition_snapshot`. Editing a draft does not mutate a previously published version. A new publish creates the next version number.

## API

All routes require an authenticated session and server-side tenant/project membership.

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/v1/tenants/{tenant_id}/projects/{project_id}/workflows/` | List/create drafts |
| GET/PATCH | `/api/v1/workflows/{workflow_id}/` | Read/update draft |
| POST | `/api/v1/workflows/{workflow_id}/publish/` | Validate and publish immutable version |
| GET/POST | `/api/v1/workflows/{workflow_id}/runs/` | List runs / enqueue a run |
| GET | `/api/v1/workflow-runs/{run_id}/` | Read run and step details |

Run creation requires `Idempotency-Key` and a JSON object in `input`. Reusing the same key and payload returns the existing run; reusing it with a different payload returns HTTP 409.

## Persistence and execution

- The task row in PostgreSQL is the durable queue record.
- Worker claims tasks atomically using row locking and a lease token.
- Each node has a unique `WorkflowStepRun` row with status, attempt count, input reference, output and error summary.
- Completed node outputs are written to the run state after each step. A subsequent attempt reuses successful steps and retries an interrupted/failed step.
- A lease expiry at the maximum attempt count marks the task and WorkflowRun terminal.
- Cancellation is cooperative. It cannot roll back a model call already completed.

## Vue Studio

The current Workflow Studio can create a draft, edit its JSON definition, save, publish, enqueue runs, poll execution history and display step statuses. The editor is a JSON editor, **not yet a drag-and-drop canvas**. The user can paste a valid definition or use the default Start → End graph.

## Not implemented in this milestone

Loop/Iterator, Parallel/Join, HTTP Request, Code sandbox, Agent node, Tool node, Knowledge Retrieval, Human Approval, Wait/Schedule, Webhook trigger, visual drag-and-drop canvas, live WebSocket run streaming, compensation handlers and workflow-level cost budgets are not implemented. No external write connector is called by this workflow engine.
