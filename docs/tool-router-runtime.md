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

## Security and current limits

The router never imports or executes plugin-supplied Python, shell, JavaScript, or arbitrary URLs. Only application-owned handlers can be registered. Credentials must be resolved by a future secret-provider adapter; connection configuration is metadata and must not contain secret values.

This milestone is an in-process trusted-handler dispatcher. It does not yet provide process/container isolation, enforced wall-clock timeouts, durable execution records, retries, HTTP/OpenAPI transport, MCP transport, OAuth refresh, or a public execute endpoint. Do not register untrusted code or handlers that perform infrastructure mutations. Those capabilities require separate sandbox and audit work.
