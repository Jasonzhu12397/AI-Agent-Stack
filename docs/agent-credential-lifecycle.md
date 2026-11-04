# Agent Node Credential Lifecycle

Agent nodes authenticate to the runtime API with a per-node bearer credential. The control plane stores only the SHA-256 digest; the raw token is returned once during enrollment or credential rotation.

## Enroll

A tenant owner or administrator creates an EPH or Target Agent node:

`POST /api/v1/tenants/{tenant_id}/projects/{project_id}/agent-nodes/`

The response contains `enrollment_token`. Deliver it to the intended host through a protected provisioning channel, configure the Agent with `AGENT_TOKEN`, and do not put it in Git, command payloads, logs, or support bundles.

## Rotate

A tenant owner or administrator can rotate the node credential:

`POST /api/v1/agent-nodes/{node_id}/credentials/rotate/`

The response contains the replacement `enrollment_token` exactly once. The previous token becomes invalid immediately, the node is marked offline, and the Agent must be restarted with the new token before it can heartbeat or poll commands again.

Rotation is rejected with HTTP 409 while the node has a running command. This prevents credential replacement from silently orphaning an active execution lease. Wait for the command to finish or reconcile its execution outcome first. Queued commands remain queued for the newly authenticated Agent.

## Revoke

`POST /api/v1/agent-nodes/{node_id}/revoke/`

Revocation is terminal. A revoked node cannot rotate its credential or authenticate again; enroll a replacement node instead.

## Operational requirements

- Use HTTPS with certificate validation for all Agent-to-control-plane traffic.
- Keep each EPH and Target Agent credential separate.
- Store the token in a root-readable secret file or an approved secret manager; avoid shell history and process arguments.
- Restrict access to the node enrollment and rotation APIs to tenant owners and administrators.
- Treat a lost token as compromised and rotate it; use revocation if the node itself is no longer trusted.
- Credential rotation does not stop a process already executing on the host. The running-command guard is therefore mandatory, and infrastructure operations with unknown outcomes require explicit reconciliation.
