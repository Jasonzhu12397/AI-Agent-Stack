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


## BMC Redfish identity verification

The Fleet machine BMC configuration endpoint stores only credential and trust-bundle references. A separate POST request to /api/v1/tenants/{tenant_id}/projects/{project_id}/fleet/machines/{machine_id}/bmc-config/ performs read-only Redfish verification.

The built-in Vault KV v2 resolver is selected automatically when BMC_VAULT_ADDR and BMC_VAULT_TOKEN are both configured and BMC_CREDENTIAL_RESOLVER is unset. Set BMC_VAULT_NAMESPACE when using Vault Enterprise namespaces and BMC_VAULT_CA_BUNDLE when Vault uses a private CA. Secret references use vault://<mount>/<path>; the credential secret must contain username and password, and an optional trust-bundle secret must contain pem or certificate. BMC_VAULT_TIMEOUT_SECONDS controls the bounded request timeout. An explicit BMC_CREDENTIAL_RESOLVER dotted Python callable overrides the built-in resolver. The platform API does not persist or log returned secret values.

The current verifier intentionally accepts literal private BMC IP addresses only, uses TLS certificate verification, disables proxy environment inheritance and redirects, applies short connection/read timeouts, limits each JSON response to 1 MiB and the Systems collection to eight members, and reads only /redfish/v1/Systems. DNS hostnames are rejected during verification to avoid DNS rebinding until a pinned-address transport is available.

Identity is marked verified only when the BMC reports exactly one System and both its UUID and serial match the Agent's previously collected DMI UUID and serial. Missing evidence remains candidate; mismatches and duplicate identities become conflict. This is a read-only identity check, not a hardware attestation or a provisioning authorization.

A deployment must provide and test its own vault-backed BMC_CREDENTIAL_RESOLVER before enabling this endpoint. The default configuration has no resolver and returns HTTP 503 without making a network request.
