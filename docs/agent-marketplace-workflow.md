# Marketplace agents in workflows

The binding layer projects an installed marketplace agent plugin onto the
existing `AgentDefinition` / `AgentVersion` records so a bound capability can
be invoked as one step of a published workflow. No new table or column is
added: every marketplace-specific detail is carried in the agent's JSON
snapshot. The mapping is data-driven; plugin keys, capability names, schemas
and risk levels all come from the stored plugin manifest, never from a vendor
table or a hard-coded endpoint.

## Binding mapping rules

One `AgentDefinition` is created per `(installation, capability)` pair.

- **Slug** is derived deterministically from `plugin_key` and `capability`:
  lowercase, non-alphanumeric runs collapsed to `-`, trimmed, truncated to
  72 characters, suffixed with a 6-character SHA-1 digest of the original
  pair. The slug is stable across repeated binds and unique within the
  project after truncation.
- **Draft config** and the published `AgentVersion.config_snapshot` carry:

  | Field | Source |
  | --- | --- |
  | `kind` | Constant `"marketplace_agent"` |
  | `plugin_key` | `AgentPluginDefinition.plugin_key` |
  | `plugin_version` | `AgentPluginInstallation.plugin_version` |
  | `installation_id` | `AgentPluginInstallation.id` |
  | `capability` | Declared capability name from the manifest |
  | `protocol` | `AgentPluginDefinition.protocol` |
  | `input_schema` | Capability declaration `input_schema` |
  | `output_schema` | Capability declaration `output_schema` |
  | `risk_level` | Capability declaration `risk_level` (default `low`) |

- **Publish**: binding creates version `1` (or the next version number after
  a refresh) with the snapshot above; `created_by` is the requesting user.
- **Rejection rules**: an installation that is `uninstalled` or `disabled`, or
  whose plugin is disabled, cannot be bound. The bind call fails with
  `BINDING_INSTALLATION_INACTIVE` and must not leave a zombie
  `AgentDefinition`. A capability not declared by the plugin fails with
  `BINDING_CAPABILITY_NOT_DECLARED`.
- **Permissions**: the bind endpoint reuses the existing tenant/project
  membership checks. Tenant members who are not project editors receive
  `403`; callers outside the tenant receive `404`, matching the rest of the
  platform API.

## Version semantics

- **Idempotent repeat**: binding the same `(installation, capability)` with
  an unchanged snapshot returns the existing definition and version. No
  duplicate definition and no duplicate version are created.
- **Plugin version drift**: when `plugin_version` (or any other snapshot
  field) differs from the latest pinned snapshot, binding adds exactly one
  new `AgentVersion`. Previously published versions are never overwritten
  or deleted; older workflow references keep resolving to their pinned
  snapshot.
- **Pinned version mismatch**: an older workflow version that pins a snapshot
  whose `plugin_version` no longer matches the installation fails at
  execution with `MARKETPLACE_AGENT_VERSION_MISMATCH`. It does not silently
  follow the upgraded installation.

## Workflow node contract

A bound marketplace agent runs inside a regular workflow `agent` node. The
engine dispatches on `config_snapshot.kind`:

- `marketplace_agent` → the capability router (`invoke_capability`);
- any other / absent kind → the existing single-turn LLM agent path, with
  behavior unchanged.

### Node configuration

| Field | Required | Description |
| --- | --- | --- |
| `agent_version_id` | Yes | UUID of the bound `AgentVersion` (existing agent node contract). |
| `capability` | Optional | Declared for cross-checking against the bound snapshot. A mismatch is rejected at validation/publish time, not at run time. |
| `arguments` | Optional | Static capability arguments, an object. |
| `arguments_from` | Optional | Name of an earlier node whose object output becomes the capability arguments. |
| `instructions` | Optional | Existing free-text role note; ignored by the marketplace path. |

When neither `arguments_from` nor a static argument object is supplied, the
node input is merged into the arguments: a dict source is merged directly,
any other scalar text is passed under the `input` key.

### Execution and output

On success the router result is stored in the step output unchanged,
augmented with `agent_id`, `agent_version`, `capability` and `plugin_key`.
The `WorkflowRun` / `WorkflowStepRun` statuses follow the existing workflow
semantics: the step is `succeeded`, the run completes successfully, and the
end node consumes the step output like any other node type.

## Failure codes

Every failure path is recorded as a failed step. The step's `error` object
carries the stable code in `error.code`; the engine never reports a silent
success or fabricates an empty result.

| Code | Meaning |
| --- | --- |
| `CAPABILITY_NOT_INSTALLED` | The bound installation is missing or was uninstalled. |
| `CAPABILITY_DISABLED` | The installation or its plugin is disabled. |
| `CAPABILITY_AUTH_REQUIRED` | The capability requires a credential that is not configured. |
| `CAPABILITY_PROTOCOL_UNSUPPORTED` | The plugin protocol has no invocation adapter. |
| `CAPABILITY_UNINSTALLED_IN_FLIGHT` | The installation was uninstalled concurrently during the call. |
| `CAPABILITY_INVOCATION_FAILED` | The adapter/transport call failed after dispatch. |
| `APPROVAL_REQUIRED` | A high/critical-risk capability was invoked without approval. |
| `MARKETPLACE_AGENT_VERSION_MISMATCH` | The pinned snapshot version no longer matches the installation. |
| `BINDING_INSTALLATION_INACTIVE` | Bind rejected: installation/plugin not active. |
| `BINDING_CAPABILITY_NOT_DECLARED` | Bind rejected: capability not declared by the plugin. |

Router codes come from `core.agent_capability_router`; binding and approval
codes are defined in `core.plugin_agent_binding`.

## Availability listing

`GET /api/v1/tenants/<tenant_id>/projects/<project_id>/marketplace-agents/`
returns every bound definition for the project, including ones whose
installation later became inactive. Each entry reports:

- definition `agent_id`, `slug`, `name`;
- `plugin_key`, `plugin_version`, `capability`, `protocol`, `risk_level`;
- current `agent_version` and `agent_version_id`;
- `installation_id` and `installation_status`;
- `available` and `availability_reason`.

Unavailable reasons are `installation_missing`, `installation_uninstalled`,
`installation_disabled`, `plugin_disabled`, `version_mismatch` and
`capability_not_granted`. Inactive entries are flagged
`available=False`; they are neither hidden nor disguised as usable.

## Behavior after uninstall or disable

1. **Listing**: the bound agent stays visible but reports `available=False`
   with the matching reason.
2. **Validation**: publish/validate only checks that the node's declared
   capability matches the bound snapshot; it does not require the
   installation to be active, so a workflow published while installed keeps
   its snapshot after a later state change.
3. **Execution**: the next run reaches the marketplace node, records the
   step as `failed` with the router code (for example
   `CAPABILITY_NOT_INSTALLED` after uninstall, `CAPABILITY_DISABLED` after
   disable), and marks the task failed. The capability is not called again,
   and no success invocation record is produced for the failed attempt.

## HTTP API

- `POST /api/v1/agent-plugin-installations/<installation_id>/bind-agent/`
  with optional body `{"capabilities": ["..."]}`. Omitting `capabilities`
  binds every capability declared by the plugin. The response lists one
  entry per bound capability: definition id, slug, name, agent version,
  capability, plugin key and version, plus whether the definition/version
  was newly created.
- `GET /api/v1/tenants/<tenant_id>/projects/<project_id>/marketplace-agents/`
  returns the availability listing above.

Routes are declared in `core/plugin_agent_urls.py` as a plain `urlpatterns`
list and are wired into the platform URL conf at integration time.

## Limits and security

- Marketplace snapshots carry no credential values; credentials remain
  references resolved server-side by the connector transport.
- Argument and result validation uses the closed-object schemas declared in
  the manifest; schema validation is enforced by the capability router.
- The marketplace path does not invoke plugin-supplied code; only registered
  protocol adapters can dispatch a call.
- High/critical-risk capabilities still require explicit approval and are
  not auto-approved by the workflow runner.
