# Agent Command History API

The Metal3 Fleet terminal uses a tenant/project-scoped aggregate endpoint instead of issuing one request per Agent node.

## Endpoint

`GET /api/v1/tenants/{tenant_id}/projects/{project_id}/agent-commands/`

Query parameters:

- `limit`: 1–200, default 100.
- `offset`: non-negative integer, default 0.
- `status`: optional AgentCommand status.
- `node_id`: optional node UUID within the tenant/project scope.
- `command_type`: optional exact command type.

The response includes `count`, `limit`, `offset` and the standard Agent command payload. Commands are ordered newest-first. Tenant membership and project membership are checked using the same access rules as Agent node inventory. The API never returns commands from another project.

The Fleet UI refreshes persisted commands every 15 seconds and labels the terminal as a polling view. It is not a WebSocket stream. Durable command logs are available from the command result and error records; streaming log transport is a separate implementation phase.

## Deployment-scoped CAPI Pivot simulation

The Supervisor creates `cluster.capi.pivot.execute` only after source and target preflight plus provider-version compatibility have passed. The command remains `WAITING_APPROVAL` until a tenant owner or administrator other than the requester approves its exact immutable `plan_hash`. The result handler independently revalidates the succeeded command state, approver identity, approval timestamp, approved plan hash and recomputed command payload hash before accepting a simulation result; a callback without that approval binding is rejected.

The Target Agent runs `clusterctl move --dry-run` and read-only source/target checks only. It must report `pivot_simulated=true`, `pivot_executed=false`, `ownership_transferred=false` and `side_effects=false`. A successful simulation does not mean the Target Agent owns the cluster: the deployment remains at `pivot_simulation_ready`, and target verification, worker provisioning and EPH reclaim are not automatically advanced. A real ownership transfer requires a separately designed and explicitly enabled execution adapter and approval policy.

## Deployment-scoped CAPI Worker provisioning

The Target Agent accepts `cluster.capi.worker.preflight` as a read-only inventory command. After the Supervisor validates the result, it creates `cluster.capi.worker.provision` itself and exposes its UUID at `deployment.plan_result.dual_agent.worker_provisioning_command_id`. Clients must not create this mutating command through `POST /api/v1/agent-nodes/{node_id}/commands/`; that endpoint explicitly rejects it so a caller cannot bypass the deployment ownership, preflight and inventory gates.

The generated command is stored as `WAITING_APPROVAL`. A tenant owner or administrator other than the requester must approve it through `POST /api/v1/agent-commands/{command_id}/approve/` with the exact immutable `plan_hash`. Approval expires after the standard short approval window. The Target Agent performs Kubernetes Server-Side Dry-Run only and returns a hashed MachineDeployment manifest preview. Real MachineDeployment writes are disabled in this runtime: the API helper rejects every request that is not `dryRun=All`, the command reports `execution_allowed=false`, `side_effects=false`, and zero created workers, and the Supervisor keeps the deployment in `plan_ready` with `worker_provisioning_simulated_manual_execution_required`. It does not advance the worker-pool checkpoint or queue postflight because no worker Machines or Nodes were created. A separate, explicitly enabled production execution adapter and its approval policy are required before any real worker provisioning can be performed.

## Mutating command lease expiry

A command that has entered `running` and whose lease expires is not automatically delivered again when it belongs to `MUTATING_COMMANDS`. The platform marks it failed with `MUTATING_COMMAND_OUTCOME_UNKNOWN_MANUAL_RECONCILIATION_REQUIRED` (or the dedicated CAPI Pivot outcome code), clears the lease, and requires an operator to inspect the target cluster before creating a new command. This is intentional at-most-once dispatch for uncertain mutating outcomes; it prevents a restarted EPH or Target Pod from blindly repeating provisioning or ownership-related actions. Read-only commands may still be retried under the configured attempt limit.

The lease expiry does not prove that the original operation failed. The terminal state means the result is unknown and must be reconciled against observed Kubernetes/CAPI/Metal3 state before proceeding.


## Deployment checkpoint resume and worker provisioning

The control plane advances the Target Agent deployment checkpoint only after it validates the corresponding command result. A validated worker preflight creates or advances the durable checkpoint to `worker_provisioning`, with `pending_step=worker_provisioning`. The Target Agent restores checkpoints while the deployment is awaiting approval or provisioning, not only during the initial handoff.

Before dispatching `cluster.capi.worker.provision`, the Target Agent requires a matching deployment ID, cluster name, generation, checkpoint phase, sequence and pending step. A missing, stale, mismatched or already-completed checkpoint fails closed before the Kubernetes mutation is attempted. After the control plane validates worker readiness, it advances the checkpoint to `worker_pool_verified`; a validated final postflight advances it to `completed`. The control plane remains authoritative for approval and deployment lifecycle state. Checkpoints are evidence for safe resume decisions, not permission to bypass approval.

Checkpoint advancement is idempotent for duplicate result processing. EPH reclaim remains disabled and is not implied by a completed worker checkpoint.


Checkpoint phase progression is monotonic. The control plane rejects unknown phases, attempts to move a checkpoint backwards, and conflicting updates within the same phase. An exact replay of an already-applied phase update remains idempotent. A late or stale result therefore cannot roll a verified worker pool back into worker provisioning.
Target Agent checkpoint sequence numbers must be contiguous: each new checkpoint must use exactly the previous sequence plus one. Gaps are rejected with HTTP 409 and include the expected and received sequence numbers; exact duplicate replay remains idempotent, while lower sequence numbers remain stale and rejected.

## The authorized command surface is the implemented command surface

`packages/agent_contracts/agent_commands.py` is what the platform authorizes. It
had drifted from what the Agents execute: the terminal listed `cluster.plan`,
`cluster.provider.discover`, `cluster.manifest.validate` and `capi.cluster.observe`
for an EPH node, and several `cluster.*` pivot aliases for a Target node that no
handler answered, so dispatching them returned `ACTION_NOT_IMPLEMENTED`.

Two changes close that gap for good:

- **Read-only diagnostics execute.** `packages/agent_contracts/readonly_commands.py`
  implements `node.health`, `host.preflight`, `cluster.health`, `cluster.observe`,
  `cluster.diagnose`, `cluster.postflight`, `cluster.network.inspect`,
  `cluster.manifest.validate` and `k8s.resource.read` once, and both Agents call it
  through their own runner. `cluster.manifest.validate` sends the documents to
  `kubectl apply --dry-run=server -f -`, so the API server validates them without
  creating anything; `k8s.resource.read` is restricted to an allowlist of namespaced
  and cluster resources.
- **The registry only names implemented actions.** The stale `cluster.*` aliases
  were removed; orchestration keeps its canonical `eph.*` / `target.*` names, and
  `cluster.plan` / `capi.cluster.observe` are real EPH handlers (fleet planning and
  the CAPI observer). `tests/test_agent_command_coverage.py` dispatches every
  authorized action through the real `execute()` of both Agents with stubbed
  binaries: a dead command now fails the build instead of the operator.

`MUTATING_COMMANDS` follows the same rule: it lists the Target actions that really
mutate (`target.capi.pivot`, `target.worker.provision`, `target.eph.cleanup`) plus
`cluster.kubernetes.version.upgrade`, so an uncertain lease expiry is treated as an
unknown mutating outcome instead of a retryable read.

## Kubernetes minor upgrade has a simulation mode and an execution mode

`packages/agent_contracts/kubernetes_upgrade.py` owns the request rules (exact
payload keys, adjacent minor only, target minor inside the supported list, and
`execution_mode` in `simulation` or `execute`). The platform validates the request
with it and the Target Agent executes it with the same function.

* `simulation` returns the four ordered, unexecuted stages — `preflight`,
  `control_plane`, `workers`, `postflight` — plus `upgrade_simulated=true`,
  `upgrade_performed=false`, `requires_manual_execution=true`,
  `resume_supported=false` and `side_effects=false`.
* `execute` runs the sequence through `apps/target_agent/kubernetes_upgrade_exec.py`:
  `kubeadm upgrade plan`, `kubeadm upgrade apply -y`, then per worker
  `kubectl drain` → `ssh <KUBERNETES_UPGRADE_REMOTE_USER>@<InternalIP> kubeadm upgrade node`
  → `kubectl uncordon`, then a postflight that rejects any node still on the old
  minor. It reports `upgrade_simulated=false`, `upgrade_performed=true`,
  `requires_manual_execution=false`, `side_effects=true` and per-step exit codes.

`execute` refuses what one agent cannot do safely: more than one control-plane
node (`UPGRADE_MULTI_CONTROL_PLANE_UNSUPPORTED`) and workers without
`KUBERNETES_UPGRADE_REMOTE_USER` (`UPGRADE_REMOTE_EXECUTOR_NOT_CONFIGURED`). It
stops at the first failed step and returns its exit code and output; there is no
rollback. Both modes still need a platform approval, and the platform accepts
execution evidence only when every stage ran to completion.
