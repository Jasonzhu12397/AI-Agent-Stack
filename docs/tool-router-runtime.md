# Tool Router runtime

`core.tool_router.ToolRouter` is the first execution boundary for Integration Hub tools.

## Execution contract

A trusted application bootstrap registers Python callables with `register(plugin_key, tool_name, handler)`. The router resolves the handler only after checking:

- the project installation is active and its pinned version matches the approved catalog version;
- the catalog connector and tool are enabled;
- every declared permission exists both in the installation grant and in the caller's permission context;
- high- and critical-risk tools carry an explicit approval decision;
- an optional connection belongs to the same installation and is healthy;
- arguments and handler results conform to the registered closed-object schema subset.

The handler receives a `ToolContext` containing tenant, project, installation, connector and optional connection metadata. It does not receive Django request objects or model instances. Handler exceptions are converted to a generic error so credential or provider details are not returned to callers.

## LLM function-calling integration

Published Agent versions can pin up to 32 `PluginTool` IDs in `tool_allowlist`. The publish API verifies that each tool belongs to an approved, enabled connector installed at the same version in the Agent's tenant/project, and requires the selected model to declare the `tools` capability.

The Worker builds OpenAI-compatible function schemas from that allowlist, sends them through `ModelGateway.complete(..., tools=...)`, validates every returned call against the offered names and bounded JSON arguments, executes calls through `ToolRouter`, appends tool results to the conversation, and asks the model to continue. The loop is bounded to 8 model iterations and 32 total tool calls. Model messages, tool-call IDs and tenant/project scopes are validated before dispatch.

High/critical risk tools remain blocked because this execution path does not yet include a persisted human-approval workflow. Tool-call errors are returned to the model as failed tool results; missing handlers never produce a fabricated success.

## Security and current limits

The router never imports or executes plugin-supplied Python, shell, JavaScript, or arbitrary URLs. Only application-owned handlers can be registered. Credentials must be resolved by a future secret-provider adapter; connection configuration is metadata and must not contain secret values.

The Worker now auto-registers a version-pinned HTTP handler for `http` and normalized `openapi` connector definitions. It accepts only HTTPS origins whose exact host is configured in `PLUGIN_HTTP_ALLOWED_HOSTS`, checks DNS results are public, disables redirects and environment proxies, enforces tool timeouts, bounds request bodies to 64 KiB and responses to 1 MiB, and requires a JSON object response. Path arguments are percent-encoded and query/body arguments must match the explicit operation mapping. A credential reference is resolved only through the configured `PLUGIN_CREDENTIAL_RESOLVER` server-side adapter; no credential value enters the browser or database.

The normalized contract is stored under `plugin.manifest.http`: `base_url`, `operations` keyed by tool name, optional `health_check`, and optional `auth_required`. Connection health checks use the same transport and mark at most one connection healthy for an installation. Handler registration is scoped to plugin key, immutable version and tool name so a new connector version cannot reuse a stale closure.

This remains an in-process trusted transport, not a plugin sandbox. DNS checks require network egress firewalling to prevent rebinding/route changes. Raw OpenAPI document import/normalization, native custom handlers, OAuth authorization/refresh, built-in Vault/KMS integration, per-call durable execution records, MCP transport, full high/critical human approvals and process/container isolation remain unfinished. No arbitrary plugin code or arbitrary URL is executed.
