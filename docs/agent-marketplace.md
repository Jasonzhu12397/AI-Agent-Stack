# Agent marketplace: plugin manifest and capability invocation

This document describes the batch 1 marketplace contract: how an agent plugin
is registered with a data-driven manifest, how installed capabilities are
resolved and invoked, how uninstall invalidates bindings, and the stable error
codes exposed to callers.

## Manifest contract

An `AgentPluginDefinition` stores a `manifest` JSON object that describes every
capability the plugin exposes. The legacy string-list `capabilities` field is
kept in sync with the manifest capability names for backwards compatibility;
the manifest is the source of truth on every read path.

```json
{
  "protocol": "http_json",
  "http": {
    "base_url": "https://market-agent.example.com",
    "operations": {
      "summarize": {
        "method": "GET",
        "path": "/v1/summarize",
        "query_params": {"text": "text"}
      }
    }
  },
  "capabilities": [
    {
      "name": "summarize",
      "description": "Summarize the supplied text",
      "input_schema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": false
      },
      "output_schema": {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": false
      },
      "risk_level": "low",
      "timeout_seconds": 30
    }
  ]
}
```

### Registration validation

The catalog POST endpoint validates the manifest before anything is persisted;
a validation failure returns `400` and leaves no definition in the database.

- Capability names match `^[a-z0-9][a-z0-9._-]{0,99}$` and are unique within the
  manifest.
- Every `input_schema` and `output_schema` must be a closed JSON object:
  `type: "object"`, `additionalProperties: false`, explicit `properties` and
  `required` arrays.
- An `http_json` plugin must declare an `http` block whose `operations` map
  covers every declared capability. Each operation uses `GET` or `POST` and a
  path rooted at a single `/`.
- `risk_level` is one of `low`, `medium`, `high`, `critical`; `timeout_seconds`
  is an integer between 1 and 300.
- The manifest is size-limited and rejects credential-like field names
  (`password`, `secret`, `token`, `api_key`, and similar).

No vendor name or endpoint is hard-coded in the platform; every host, path and
operation mapping comes from the stored manifest.

## Resolution and invocation

`core.agent_capability_router` provides two entry points:

- `resolve_capability(tenant_id, project_id, capability_name)` returns a
  `ResolvedCapability` dataclass exposing `installation`, `plugin`, `definition`
  and `capability_name`. It never falls back to an empty implementation or
  `None`; every failure raises `CapabilityResolutionError` with a stable
  `code`.
- `invoke_capability(tenant_id, project_id, capability_name, arguments, approved=False)`
  re-checks the installation, validates arguments against the input schema,
  dispatches through the registered protocol adapter, validates the result
  against the output schema, re-checks the installation after the call and
  records an invocation outcome.

### Deterministic capability selection

When more than one active installation declares the same capability, the
winner is chosen by stable, reproducible ordering:

1. Plugin version, segment by segment: numeric segments compare numerically
   and win over non-numeric segments; non-numeric segments compare
   lexicographically.
2. `created_at` descending (the most recent installation wins).
3. Installation id lexicographic ascending, as the final tie-break.

### Error codes

| Code | Meaning |
|---|---|
| `CAPABILITY_NOT_DECLARED` | No plugin in the tenant declares the capability, or the capability was not granted to the installation. |
| `CAPABILITY_NOT_INSTALLED` | The capability is declared but no active installation exists in the project, or the installation was uninstalled. |
| `CAPABILITY_DISABLED` | The installation or its plugin definition is disabled. |
| `CAPABILITY_PROTOCOL_UNSUPPORTED` | The plugin protocol has no registered adapter. `mcp` and `a2a` are reserved for a later batch. |
| `CAPABILITY_AUTH_REQUIRED` | The plugin requires a credential reference but none is bound to the installation. |
| `CAPABILITY_UNINSTALLED_IN_FLIGHT` | The installation changed to uninstalled or disabled during the call; the result must not be reported as success. |
| `CAPABILITY_INVOCATION_FAILED` | The transport call, schema validation or adapter failed. |

`high` and `critical` risk capabilities require `approved=True`; otherwise the
call is rejected and recorded as `rejected` without invoking the adapter.

## Install and uninstall semantics

- Install POST is idempotent for a given project and plugin:
  - A missing row is created and returns `201`.
  - An existing `installed` row returns `200` unchanged.
  - An existing `disabled` row returns `200` without changing its state or
    grants.
  - An existing `uninstalled` row is revived to `installed`: the plugin
    version, granted capabilities, credential reference and installing user
    are refreshed, and the response returns `200`. No duplicate row is created.
- The request body identifies the plugin by `plugin_id` (definition primary
  key) or by `plugin_key`, optionally paired with `plugin_version`; both
  missing returns `400`.
- The status endpoint treats `uninstalled` as a terminal state. Repeating an
  uninstall is idempotent and returns `200`.
- On transition to `uninstalled`, the status endpoint runs registered
  invalidation hooks (`invalidate_installation`). The worker router drops
  bound in-process handlers; resolution itself reads the database on every
  call, so an uninstalled capability is immediately unresolvable. Cross-process
  propagation is not required in this batch.

## Invocation records

Each attempt creates an `AgentCapabilityInvocation` row linked to the
installation, with the capability name, protocol, outcome status
(`success`, `failed` or `rejected`), stable error code and latency in
milliseconds. Arguments and response bodies are never persisted. The read-only
capability API exposes the latest invocation per installation and capability.

## API

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/v1/tenants/{tenant_id}/agent-plugins/` | List/register tenant agent plugin definitions |
| GET | `/api/v1/agent-plugins/{plugin_id}/` | Read a plugin definition and manifest |
| GET/POST | `/api/v1/tenants/{tenant_id}/projects/{project_id}/agent-plugin-installations/` | List/install project installations |
| POST | `/api/v1/agent-plugin-installations/{installation_id}/status/` | Enable, disable or uninstall an installation |
| GET | `/api/v1/tenants/{tenant_id}/projects/{project_id}/agent-capabilities/` | List active capabilities visible to the project |
| GET | `/api/v1/tenants/{tenant_id}/marketplace/catalog/` | Browse registered plugins with per-project installation state |
| GET | `/api/v1/tenants/{tenant_id}/marketplace/catalog/{plugin_key}/` | Read one catalog entry with full manifest capability detail |

The capability list returns only `installed` installations of enabled plugins.
Each entry carries `name`, `plugin_key`, `plugin_version`, `protocol`,
`risk_level`, `installation_id`, `input_schema` and `last_invocation`
(`{status, error_code, created_at}` or `null`).

## Read-only marketplace catalog

`GET /api/v1/tenants/{tenant_id}/marketplace/catalog/` lets the frontend browse
registered plugin definitions before installing them. The endpoint is a pure
GET: it writes nothing to the database and never triggers remote discovery or
synchronization.

### Query parameters

- `project_id` (UUID, optional): installation scope. When omitted, every
  entry is reported as not installed in the scope (`installed=false`,
  `installation_status=not_installed`, `installation_id=null`,
  `project_id=null`); the catalog can be opened from the browser without a
  project context. An unknown or inaccessible project returns `404`.
- `capability`: case-insensitive substring match against declared capability
  names. As a fallback it also matches the plugin `name` and `description`.
- `protocol`: exact match against a registered protocol (`http_json`, `mcp`,
  `a2a`); an unknown value returns `400`.
- `installed`: `true` or `false`; filters on whether an installation exists
  in the scoped project with a status other than `uninstalled`. Any other
  value returns `400`.

### Catalog entry contract

```json
{
  "plugin_key": "market-agent-a",
  "name": "Market Agent A",
  "description": "Summarizes research reports",
  "version": "1.2.0",
  "protocol": "http_json",
  "enabled": true,
  "auth_required": true,
  "capabilities": [
    {"name": "summarize", "description": "Summarize the supplied text", "risk_level": "low"}
  ],
  "installed": true,
  "installation_status": "installed",
  "installation_id": "<uuid>",
  "project_id": "<uuid>"
}
```

`installation_status` is one of `installed`, `disabled`, `uninstalled`,
`not_installed`; `installation_id` and `project_id` are `null` when no project
scope is resolved. `auth_required` comes from the definition and tells the
browse-to-install flow whether a credential reference must be collected.

### Aggregation and scope

- Definitions in the tenant are grouped by `plugin_key`; the highest version
  wins, using the same semver-like segment ordering as the capability router,
  with most recent `created_at` as the tie-break.
- Installation state is evaluated only against the requested project. An
  installation in another project does not mark the entry installed in this
  project's scope.

### Detail endpoint

`GET /api/v1/tenants/{tenant_id}/marketplace/catalog/{plugin_key}/` returns the
same entry for a single plugin, with full manifest capability detail
(`input_schema`, `output_schema`, `timeout_seconds`) and the stored
`manifest`, `input_schema` and `output_schema`. An unknown `plugin_key`
returns `404`.

### Permissions

Any tenant member may browse the catalog; the endpoint is read-only and does
not require an admin role. Resolving `project_id` follows project membership:
a project the user cannot access returns `404`.

## Not implemented in this batch

- `mcp` and `a2a` protocol adapters: the adapter registry interface exists, but
  calls to those protocols surface `CAPABILITY_PROTOCOL_UNSUPPORTED`.
- Credential resolution: credential-backed HTTP calls still require a configured
  resolver; plugin registration and installation accept only a `credential_ref`
  reference, never a secret value.
- Agent definition binding: linking marketplace capabilities to `AgentDefinition`
  records is handled in a later batch.
