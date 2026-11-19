# Cloud Control project workspace

The Cloud Control Agent is not only a deployment switchboard. It also owns a
project workspace where operators register reusable skills, plugins and work,
and Agents claim that work over their existing mTLS identity.

The control plane stores manifests and references, never implementations or
credentials. Skill code runs on the Agent that claims the work; plugin
credentials stay behind `config://`, `secret://` or `vault://` references.

## Scope model

| Object | Scope | Purpose |
| --- | --- | --- |
| `agent_control_projects` | one installation | Container for skills, plugins and work |
| `agent_control_skills` | one project | Versioned manifest with an entrypoint and dotted actions |
| `agent_control_plugins` | one project | Connector, tool or runtime registration with an indirect config reference |
| `agent_control_work_items` | one project | Durable unit of work with priority, role affinity, lease and result |
| `agent_control_tasks` | one project | Standalone or workflow-step task with dependencies, lease and result |
| `agent_control_workflows` | one project | Versioned workflow definition with a mutable latest version pointer |
| `agent_control_workflow_versions` | one workflow | Immutable JSONB DAG snapshot per version |
| `agent_control_workflow_runs` | one workflow | Run instance with input, status and aggregated output |

Projects are unique per installation by name. Skill and plugin names are unique
per project and version, so a new version is a new row rather than an in-place
mutation. A project can be `active` or `archived`; archived projects cannot
accept new skills, plugins or work.

## Platform API

All project endpoints require the dedicated `platform-api` client certificate.

```text
POST /api/v1/control/projects
GET  /api/v1/control/projects/{project_id}
POST /api/v1/control/projects/{project_id}/skills
GET  /api/v1/control/projects/{project_id}/skills
POST /api/v1/control/projects/{project_id}/plugins
GET  /api/v1/control/projects/{project_id}/plugins
POST /api/v1/control/projects/{project_id}/work
GET  /api/v1/control/projects/{project_id}/work
GET  /api/v1/control/work/{work_id}
POST /api/v1/control/work/{work_id}/cancel

POST /api/v1/control/projects/{project_id}/tasks
GET  /api/v1/control/projects/{project_id}/tasks
GET  /api/v1/control/tasks/{task_id}

POST /api/v1/control/projects/{project_id}/workflows
GET  /api/v1/control/projects/{project_id}/workflows
GET  /api/v1/control/workflows/{workflow_id}
POST /api/v1/control/workflows/{workflow_id}/versions
POST /api/v1/control/workflows/{workflow_id}/runs
GET  /api/v1/control/workflow-runs/{run_id}
POST /api/v1/control/workflow-runs/{run_id}/cancel
```

Create a project:

```json
{
  "project_id": "00000000-0000-0000-0000-000000000001",
  "installation_id": "00000000-0000-0000-0000-000000000002",
  "name": "edge-lab",
  "description": "Bare-metal lab",
  "metadata": {"region": "lab"}
}
```

Register a skill:

```json
{
  "skill_id": "00000000-0000-0000-0000-000000000003",
  "name": "metal3-prep",
  "version": "1.0.0",
  "manifest": {
    "entrypoint": "skills.metal3.prepare",
    "actions": ["bmh.apply", "bmh.wait_ready"]
  }
}
```

Register a plugin. `config_ref` must be an indirect reference; inline
credentials are rejected:

```json
{
  "plugin_id": "00000000-0000-0000-0000-000000000004",
  "name": "vault",
  "version": "2.0.0",
  "kind": "connector",
  "config_ref": "secret://tenants/edge/vault"
}
```

Enqueue work:

```json
{
  "work_id": "00000000-0000-0000-0000-000000000005",
  "kind": "inspection",
  "required_role": "eph",
  "priority": 5,
  "payload": {"cluster_name": "edge-01"},
  "idempotency_key": "inspection-edge-01-001"
}
```

## Agent API

Agents use the same certificate-bound identity as heartbeats and commands:

```text
POST /api/v1/agent-control/work/claim
POST /api/v1/agent-control/work/lease/renew
POST /api/v1/agent-control/work/result
POST /api/v1/agent-control/tasks/claim
POST /api/v1/agent-control/tasks/lease/renew
POST /api/v1/agent-control/tasks/result
```

`claim` selects queued work, or work whose lease expired, for any active project
in the Agent's installation. It filters on `required_role` when set, orders by
priority and age, and uses `FOR UPDATE SKIP LOCKED` so concurrent Agents cannot
claim the same item. A successful claim returns:

```json
{
  "work": {
    "work_id": "...",
    "project_id": "...",
    "project_name": "edge-lab",
    "kind": "inspection",
    "priority": 5,
    "payload": {"cluster_name": "edge-01"}
  },
  "skills": [{"name": "metal3-prep", "version": "1.0.0", "manifest": {...}}],
  "plugins": [{"name": "vault", "kind": "connector", "config_ref": "secret://..."}],
  "lease_token": "...",
  "lease_seconds": 90
}
```

The lease token is returned once and stored only as a SHA-256 hash. Renewal and
result submission require the same certificate identity, and results are only
accepted while the item is leased to that Agent. `succeeded` and `failed` are
terminal; expired leases become claimable again and increment `attempts`.

The transport client exposes `claim_work()`, `renew_work_lease()`,
`report_work_result()` and the matching `claim_task()`, `renew_task_lease()`,
`report_task_result()` methods in `packages/agent_transport/client.py`.

## Workflows

A workflow definition is a JSONB DAG with one to 32 steps:

```json
{
  "description": "Prepare then verify",
  "steps": [
    {"id": "prepare", "name": "Prepare hosts", "required_role": "eph"},
    {
      "id": "verify",
      "name": "Verify hosts",
      "depends_on": ["prepare"],
      "payload": {"checks": ["bmh", "k8s"]}
    }
  ]
}
```

Creating a workflow publishes version 1. `POST /workflows/{id}/versions`
appends a new immutable version and advances `latest_version`. Existing runs
keep the version they started with; they are not retroactively edited.

Starting a run materializes one task per step. Steps without dependencies are
`queued`; steps with dependencies are `blocked`. When an Agent reports a task
`succeeded`, blocked tasks whose dependencies all succeeded become `queued`. A
failed task marks the run `failed` and cancels every remaining blocked, queued or
leased task. When no active task remains, the run becomes `succeeded` and its
output is the JSON object of `step_id -> task result`.

Task claims follow the same rules as work claims: installation scope, active
project, role affinity, priority, `FOR UPDATE SKIP LOCKED`, 90-second lease and
a one-time token stored as SHA-256. Results and renewals are bound to the
certificate identity and require the active token.

## Limits

- Skill and plugin manifests are capped at 64 KiB; work payloads and results at
  256 KiB.
- Work kinds are `deployment`, `maintenance`, `inspection` and `diagnostic`.
- Work priority is an integer from 0 to 9.
- The control plane does not execute skills, install plugins or fetch plugin
  credentials. It records the catalog, dispatches work and retains audit state.
