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

## Deployment-scoped CAPI Worker provisioning

The Target Agent accepts `cluster.capi.worker.preflight` as a read-only inventory command. After the Supervisor validates the result, it creates `cluster.capi.worker.provision` itself and exposes its UUID at `deployment.plan_result.dual_agent.worker_provisioning_command_id`. Clients must not create this mutating command through `POST /api/v1/agent-nodes/{node_id}/commands/`; that endpoint explicitly rejects it so a caller cannot bypass the deployment ownership, preflight and inventory gates.

The generated command is stored as `WAITING_APPROVAL`. A tenant owner or administrator other than the requester must approve it through `POST /api/v1/agent-commands/{command_id}/approve/` with the exact immutable `plan_hash`. Approval expires after the standard short approval window. The Target Agent then performs a Kubernetes Server-Side Dry-Run followed by an idempotent MachineDeployment create, and waits for the Machines and their Nodes to become Ready. The Supervisor validates the command approval identity and returned inventory before queuing the independent postflight check.

## Mutating command lease expiry

A command that has entered `running` and whose lease expires is not automatically delivered again when it belongs to `MUTATING_COMMANDS`. The platform marks it failed with `MUTATING_COMMAND_OUTCOME_UNKNOWN_MANUAL_RECONCILIATION_REQUIRED` (or the dedicated CAPI Pivot outcome code), clears the lease, and requires an operator to inspect the target cluster before creating a new command. This is intentional at-most-once dispatch for uncertain mutating outcomes; it prevents a restarted EPH or Target Pod from blindly repeating provisioning or ownership-related actions. Read-only commands may still be retried under the configured attempt limit.

The lease expiry does not prove that the original operation failed. The terminal state means the result is unknown and must be reconciled against observed Kubernetes/CAPI/Metal3 state before proceeding.


## Deployment checkpoint resume and worker provisioning

The control plane advances the Target Agent deployment checkpoint only after it validates the corresponding command result. A validated worker preflight creates or advances the durable checkpoint to `worker_provisioning`, with `pending_step=worker_provisioning`. The Target Agent restores checkpoints while the deployment is awaiting approval or provisioning, not only during the initial handoff.

Before dispatching `cluster.capi.worker.provision`, the Target Agent requires a matching deployment ID, cluster name, generation, checkpoint phase, sequence and pending step. A missing, stale, mismatched or already-completed checkpoint fails closed before the Kubernetes mutation is attempted. After the control plane validates worker readiness, it advances the checkpoint to `worker_pool_verified`; a validated final postflight advances it to `completed`. The control plane remains authoritative for approval and deployment lifecycle state. Checkpoints are evidence for safe resume decisions, not permission to bypass approval.

Checkpoint advancement is idempotent for duplicate result processing. EPH reclaim remains disabled and is not implied by a completed worker checkpoint.
