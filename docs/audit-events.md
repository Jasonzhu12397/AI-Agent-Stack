# Tenant Audit Events

## Scope

The platform API stores selected security-sensitive mutations as tenant-scoped append-only audit events. The initial coverage includes:

- Tenant member creation, role changes and removal.
- Agent node enrollment and revocation.
- Agent command queueing, approval requests and approvals.
- Connector approval, installation, installation status changes, connection health checks and connection enable/disable changes.
- Cluster deployment creation, cancellation and Target Agent handoff requests.

The event writer intentionally stores identifiers and bounded operational metadata only. Credential-like keys are removed recursively. It does not store command payloads, model prompts, connector secrets, enrollment tokens or kubeconfig data.

## Integrity model

Each tenant has an independent SHA-256 hash chain. The event hash covers:

- Event and tenant IDs.
- Project and actor IDs.
- Event type, resource type and resource ID.
- Outcome, request ID, remote address and user agent.
- Sanitized metadata.
- Previous event hash and exact event timestamp.

The writer locks the tenant row while selecting the prior hash and appending the next event. This serializes concurrent writers for one tenant in PostgreSQL.

The application model rejects ordinary updates and deletes. Actor, project and tenant references use protected deletion behavior so foreign-key cleanup cannot silently rewrite hashed event identity fields.

## API

- `GET /api/v1/tenants/{tenant_id}/audit-events/?limit=100&offset=0`
- `GET /api/v1/tenants/{tenant_id}/audit-events/verify/`

Both endpoints require an authenticated tenant owner or administrator. The list endpoint optionally accepts `project_id`. The verifier walks the tenant chain from its beginning and returns the first invalid event, if any.

## Limitations and production requirements

This is tamper-evident application-level logging, not an external immutable archive. A database administrator able to rewrite every row and recompute the chain can defeat an unanchored hash chain. Deleting the latest tail events is not detectable unless the latest hash is anchored outside the database.

Before production use, add:

1. External periodic head-hash anchoring or signed checkpoints.
2. Independent WORM/object-lock export and retention policy.
3. Coverage for denied operations, authentication events, model/Agent/workflow publishing, connector connection changes and administrative settings.
4. Pagination and verification limits suitable for large tenants.
5. Backup/restore integrity verification and operational alerting.
6. Explicit privacy and retention policy for IP address and user-agent fields.

The UI labels current event coverage and hash-chain limitations rather than claiming complete compliance-grade immutability.
