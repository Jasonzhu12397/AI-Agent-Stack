# Integration Hub: plugin registry milestone

## Implemented control-plane capabilities

The current plugin slice provides tenant-scoped connector metadata and project-scoped installations.

- Register a versioned connector definition with type `native`, `mcp`, `http`, `openapi` or `custom`.
- Register 1–50 tool contracts with closed object input/output schemas, permission declarations, risk level, timeout, retry policy and idempotency mode.
- New versions start disabled in `DRAFT`; a tenant owner/admin must approve the metadata before installation.
- Install an approved connector into a project with an explicit subset of the permissions declared by its tools.
- Pin the installed plugin version. An upgrade requires an explicit uninstall/reinstall path; the installation is not silently moved to a newer version.
- Create named connection records using `credential_ref` or `oauth_ref` URIs. The platform stores only the reference, never a credential value.
- Disable or uninstall a project installation. Uninstall disables its connection records.
- The Vue Integration Hub shows catalog entries, tool risk/permission metadata, review state, project installations and credential references.

## API

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/v1/connectors/?tenant_id=...` | List/register tenant connector definitions |
| GET | `/api/v1/connectors/{plugin_id}/` | Read connector and tool contracts |
| POST | `/api/v1/connectors/{plugin_id}/approve/` | Tenant admin review |
| GET/POST | `/api/v1/tenants/{tenant_id}/projects/{project_id}/plugin-installations/` | List/install project connectors |
| POST | `/api/v1/plugin-installations/{installation_id}/status/` | Enable, disable or uninstall |
| GET/POST | `/api/v1/plugin-installations/{installation_id}/connections/` | List/create connection references |

## Security constraints

- Tenant and project identity is derived from the authenticated session and server-side membership.
- Plugin metadata, tool schemas and connection config are size-limited and reject sensitive credential fields.
- Input/output schemas must be closed JSON objects; `additionalProperties: true` is rejected.
- A non-idempotent tool must have `max_attempts=1`; retry attempts are capped at 3 and timeout at 300 seconds.
- Permission grants must be a subset of the tool permissions declared by the plugin.
- Credentials are represented only by a URI such as `vault://path/to/credential`; plaintext tokens, passwords and API keys are not accepted.
- Registering or installing a connector does not execute it and does not imply permission to make external API calls.

## Not implemented in this milestone

The plugin registry does not yet run plugin code. Plugin Executor isolation, HTTP/OpenAPI request execution, OAuth authorization flows, credential broker integration, connector health checks, MCP transport, provider-specific GitHub/Jira/Graph/Maps/Weather clients and runtime policy enforcement remain future phases. Do not describe an installed connector as operational until its executor and contract tests exist.
