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
| `agent_control_work_items` | one project | Durable unit of work with priority, role affinity, lease and result; kinds are `deployment`, `maintenance`, `inspection`, `diagnostic` and `software` |
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

GET  /api/v1/control/projects/{project_id}/approvals
GET  /api/v1/control/approvals/{approval_id}
POST /api/v1/control/projects/{project_id}/approvals
POST /api/v1/control/approvals/{approval_id}/approve
POST /api/v1/control/approvals/{approval_id}/reject
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

## Patch approvals

A patch approval binds one `work_id`, one action and one SHA-256 payload digest.
There is exactly one approver: a `pending` request becomes `approved` when one
operator posts `approved_by`. No second approver and no quorum rule exist.

```json
{
  "approval_id": "00000000-0000-0000-0000-000000000006",
  "work_id": "00000000-0000-0000-0000-000000000005",
  "action": "apply-patch",
  "payload_digest": "6f1e...64 hex characters...",
  "requested_by": "alice",
  "ttl_seconds": 600
}
```

Approve or reject:

```json
{"approved_by": "bob"}
```

```json
{"approved_by": "bob", "reason": "superseded by work-7"}
```

`payload_digest` is the lowercase SHA-256 of the canonical JSON form of the
`edits` list (`sort_keys=True`, `separators=(",", ":")`). TTL is 60–86400
seconds and defaults to 3600. Expired approvals cannot be approved or redeemed.

Approvals gate **software patch work only**. EPH and Target agents remain
autonomous: they install Kubernetes and repair their mounted repository without
a control-plane approval, and the approval table is never consulted for their
deployment work. See `docs/dev-agent.md` for their autonomy boundary.

## Agent API

Agents use the same certificate-bound identity as heartbeats and commands:

```text
POST /api/v1/agent-control/work/claim
POST /api/v1/agent-control/work/lease/renew
POST /api/v1/agent-control/work/result
POST /api/v1/agent-control/tasks/claim
POST /api/v1/agent-control/tasks/lease/renew
POST /api/v1/agent-control/tasks/result
POST /api/v1/agent-control/approvals/redeem
```

`claim` selects queued work, or work whose lease expired, for any active project.
It accepts an optional `"kind"` field so a software executor asks only for
`kind: "software"` and cannot lease deployment work.
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
`redeem_approval()` posts to the redeem endpoint and requires `"redeemed": true`.

Redemption is an atomic `UPDATE ... SET status = 'used'` guarded by the Agent
mTLS identity, the work lease, the action and the payload digest. A stale,
re-used or mismatched approval returns `APPROVAL_NOT_REDEEMABLE`, so a digest
cannot be replayed for different edits or a different work item.

## Workflows

A workflow definition is a JSONB DAG with one to 32 steps:

```json
{
  "description": "Prepare then verify",
  "steps": [
    {"id": "prepare", "name": "Prepare hosts", "required_role": "eph", "max_attempts": 2},
    {
      "id": "review",
      "name": "Review host inventory",
      "type": "approval",
      "depends_on": ["prepare"],
      "payload": {"instructions": "Confirm the BMH list before apply"}
    },
    {
      "id": "verify",
      "name": "Verify hosts",
      "depends_on": ["review"],
      "payload": {"checks": ["bmh", "k8s"]}
    }
  ]
}
```

Every step also accepts `join`: `all` (default) or `any`. With `all`, a blocked
step runs only after every dependency succeeded. With `any`, it becomes runnable
as soon as one dependency succeeds, so parallel branches can merge on the first
result while the remaining branches keep running. A failed dependency still
fails the run, so `any` never lets a run succeed around a failure.

A step `type` is `task` (default), `approval`, `loop` or `condition`. A
condition step is evaluated by the control plane and never claimed by an Agent.
It reads either the workflow input or an earlier step result:

```json
{
  "id": "check",
  "name": "Check readiness",
  "type": "condition",
  "depends_on": ["inspect"],
  "condition": {
    "source": "inspect",
    "field": "readiness.ready",
    "equals": true
  }
}
```

`source` is optional (omit it to read the workflow input), must be a dependency,
and `field` is a dotted path. `equals` must be a scalar; the comparison is
type-strict, so `true` does not match `1`. The step result is
`{"matched": bool, "found": bool, "field": "...", "source": "..."}`.

Branches declare `when` per condition dependency:

```json
{"id": "apply", "depends_on": ["check"], "when": {"check": true}}
{"id": "skip", "depends_on": ["check"], "when": {"check": false}}
```

A branch is cancelled when its expected value differs from `matched`; cancelled
branches propagate to blocked descendants that can no longer succeed. A branch
does not run before the referenced condition has actually succeeded.

A step `type` is `task` (default), `approval` or `loop`. A loop step repeats the
same agent task within a mandatory budget of 2–10 iterations:

```json
{
  "id": "wait",
  "name": "Wait for BMH",
  "type": "loop",
  "max_iterations": 5,
  "required_role": "eph"
}
```

Each claim exposes the current `iteration` and `loop_max_iterations`. The Agent
requests another iteration by returning exactly:

```json
{"continue": true}
```

Any other successful result finishes the step, and reaching
`loop_max_iterations` also finishes it even if `continue` is still `true`. A
failed iteration follows the normal `max_attempts` retry rules; loops and retries
are independent counters. `max_iterations` is rejected on non-loop steps. An approval step must not set
`required_role` and must keep `max_attempts` at 1. It is never claimable by an
Agent: when its dependencies succeed it moves to `awaiting_approval`, and the
run stays `running` until a platform operator posts a decision:

```json
{"decided_by": "bob", "reason": "optional rejection reason"}
```

`approve` marks the step `succeeded`, records `{decision, decided_by, reason}`
as its result and immediately unblocks its dependents. `reject` marks the step
`failed`, fails the run with `{failed_step, error, decision}` in `output`, and
cancels every remaining `blocked`, `queued`, `leased` or `awaiting_approval`
task. Approval decisions require the `platform-api` certificate; Agent
certificates cannot decide workflow steps.

Creating a workflow publishes version 1. `POST /workflows/{id}/versions`
appends a new immutable version and advances `latest_version`. Existing runs
keep the version they started with; they are not retroactively edited.

Each run carries an absolute deadline. `POST /workflows/{id}/runs` accepts
`deadline_seconds` from 60 to 604800 and defaults to 86400. The response exposes
`deadline_at`. `POST /api/v1/control/workflows/reconcile` (and the periodic
`--reconcile-once` job) fails running runs whose deadline passed with
`output.error = "WORKFLOW_DEADLINE_EXCEEDED"` and cancels every active task,
including `awaiting_approval` steps.

Starting a run materializes one task per step. Steps without dependencies are
`queued` (or `awaiting_approval` for approval steps); steps with dependencies are
`blocked`. When a step succeeds, blocked `all`-join tasks whose dependencies all
succeeded become `queued`, blocked `any`-join tasks with at least one succeeded
dependency become `queued`, and approval steps become `awaiting_approval`. A
failed task is retried while its `max_attempts` budget (1–5, default 1) is not
exhausted: the control plane clears the result, returns it to `queued` and keeps
the run `running`, reporting `"retrying": true`. Once the budget is exhausted the
run becomes `failed` and every remaining blocked, queued or leased task is
cancelled. When no active task remains, the run becomes `succeeded` and its
output is the JSON object of `step_id -> task result`.

Task claims follow the same rules as work claims: installation scope, active
project, role affinity, priority, `FOR UPDATE SKIP LOCKED`, 90-second lease and
a one-time token stored as SHA-256. Results and renewals are bound to the
certificate identity and require the active token.

## Limits

- Skill and plugin manifests are capped at 64 KiB; work payloads and results at
  256 KiB.
- Work kinds are `deployment`, `maintenance`, `inspection`, `diagnostic` and
  `software`.
- Work priority is an integer from 0 to 9.
- The control plane does not execute skills, install plugins or fetch plugin
  credentials. It records the catalog, dispatches work and retains audit state.
