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

The Worker now has an application-owned HTTP/OpenAPI contract executor for connectors that provide a normalized `manifest.http` contract. It requires HTTPS, an exact host in `PLUGIN_HTTP_ALLOWED_HOSTS`, public DNS results, no redirects, no environment proxy, bounded request/response sizes, JSON object responses and tool-schema validation. The Integration Hub can test a configured `manifest.http.health_check` and only then mark one connection healthy. Credential-backed calls require a configured `PLUGIN_CREDENTIAL_RESOLVER` dotted path; the resolver receives the credential reference and returns a supported bearer or header credential. No secret value is persisted.

A connector manifest can define:
```json
{
  "http": {
    "base_url": "https://api.example.com",
    "auth_required": false,
    "health_check": {"method": "GET", "path": "/health"},
    "operations": {
      "read-status": {
        "method": "GET",
        "path": "/v1/status/{resource_id}",
        "path_params": {"resource_id": "resource_id"},
        "query_params": {}
      }
    }
  }
}
```

The `openapi` connector type currently uses the same normalized operation map; importing and compiling an arbitrary OpenAPI document is not yet implemented. Native/custom application handlers still need trusted registration. OAuth authorization/refresh, a built-in Vault/KMS resolver, process/container sandbox isolation, MCP transport, provider-specific GitHub/Jira/Graph/Maps/Weather clients and full high/critical human approvals remain future phases. DNS validation is not a substitute for network egress firewalling or external sandboxing.
