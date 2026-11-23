# Agent protocol adapters: MCP, OpenAI-compatible and A2A

Phase 2 of the agent marketplace adds a protocol adapter layer in front of the
capability router. Each adapter translates a remote protocol endpoint into the
platform capability model: a discovered or declared capability is resolved by
`core.agent_capability_router.resolve_capability`, validated against its closed
JSON Schema, dispatched through the registered adapter, and the result is
validated again before an invocation outcome is recorded.

Adapters are registered in `core.apps.CoreConfig.ready()`; an import or
registration failure surfaces during startup instead of being swallowed by a
fallback handler.

| Protocol string | Adapter module | Remote surface |
|---|---|---|
| `mcp` | `core.agent_mcp_adapter` | Streamable HTTP MCP: `initialize` handshake, paged `tools/list`, `tools/call` |
| `openai_compatible` | `core.agent_openai_compatible_adapter` | `POST {base_url}{path}` chat completions |
| `a2a` | `core.agent_a2a_adapter` | Agent card discovery over `GET {origin}{agent_card_path}`, JSON-RPC 2.0 `message/send` |

The `openai_compatible` protocol string is used directly even though the
`AgentPluginDefinition.Protocol` choices enum is extended in a follow-up batch;
no model or migration change is introduced here.

## Manifest contracts

### MCP

The MCP endpoint is taken from `plugin.endpoint_url`. The optional `mcp`
manifest block extends the transport with tool mapping, risk overrides and a
timeout:

```json
{
  "mcp": {
    "tool_map": {"local-tool-name": "remote-tool-name"},
    "risk_levels": {"remote-tool-name": "medium"},
    "timeout_seconds": 30
  },
  "capabilities": [
    {
      "name": "fetch_weather",
      "description": "Fetch the current weather",
      "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
        "additionalProperties": false
      },
      "output_schema": {
        "type": "object",
        "properties": {"text": {"type": "string"}, "data": {"type": "object"}},
        "required": [],
        "additionalProperties": false
      },
      "risk_level": "low",
      "timeout_seconds": 30,
      "remote_name": "fetch-weather"
    }
  ]
}
```

### OpenAI-compatible

No vendor base URL, model family or provider is hard-coded; every value comes
from the stored manifest:

```json
{
  "openai_compatible": {
    "base_url": "https://example-host.example.com",
    "model": "example-model",
    "path": "/v1/chat/completions",
    "timeout_seconds": 60
  },
  "capabilities": [
    {
      "name": "complete",
      "description": "Complete a prompt",
      "input_schema": {
        "type": "object",
        "properties": {"prompt": {"type": "string"}},
        "required": ["prompt"],
        "additionalProperties": false
      },
      "output_schema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": false
      },
      "risk_level": "low",
      "timeout_seconds": 60
    }
  ]
}
```

The request body is `{"model": ..., "messages": [...], "stream": false}`.
Messages are taken from `arguments.messages` when present; otherwise a single
user message is assembled from `arguments.prompt` (falling back to
`arguments.message`). The result is normalized to
`{"text": choices[0].message.content}`.

Completion options are validated locally before any request is sent:

- `max_tokens`: must be a positive integer (booleans do not count as numbers).
  The upper bound is read from `manifest.openai_compatible.max_tokens_limit`
  when it is a positive integer; otherwise the documented default ceiling of
  `4096` applies.
- `temperature`: must be a finite number in `[0, 2]`; booleans do not count as
  numbers.
- Omitted options are not invented; explicit legal values are preserved
  verbatim in the request body.

An out-of-range or non-numeric option is rejected locally with
`CAPABILITY_ARGUMENTS_OUT_OF_RANGE`; the illegal value never reaches the
upstream endpoint. The optional `max_tokens` and `temperature` arguments are
reserved protocol options recognized by the adapter.

### A2A

```json
{
  "a2a": {
    "agent_card_path": "/.well-known/agent-card.json",
    "timeout_seconds": 60
  },
  "capabilities": [
    {
      "name": "greet",
      "description": "Greet a traveler",
      "input_schema": {
        "type": "object",
        "properties": {"prompt": {"type": "string"}},
        "required": ["prompt"],
        "additionalProperties": false
      },
      "output_schema": {
        "type": "object",
        "properties": {"text": {"type": "string"}, "data": {"type": "object"}},
        "required": [],
        "additionalProperties": false
      },
      "risk_level": "low",
      "timeout_seconds": 60,
      "remote_name": "greet"
    }
  ]
}
```

## Capability mapping rules

### MCP discovery

`discover(installation)` performs the `initialize` handshake and fetches every
`tools/list` page until no `nextCursor` is returned. Each remote tool is mapped
to a capability:

- `name`: the remote tool name when it already matches
  `^[a-z0-9][a-z0-9._-]{0,99}$`; otherwise it is lower-cased, invalid
  characters are replaced with `_`, and the identifier is trimmed to the bounded
  shape. The original remote name is preserved as `remote_name` on the
  capability declaration.
- `description`: the tool description, truncated to 1,000 characters.
- `input_schema`: the tool input schema converted to a closed JSON object
  (`additionalProperties: false`, explicit `properties` and `required`). Tools
  whose input schema is not an object type are rejected and skipped; the rest of
  the tool list is still processed.
- `output_schema`: a two-shape closed object exposing optional `text` and
  `data` properties.
- `risk_level`: `low` by default, overridable per remote tool through
  `manifest.mcp.risk_levels`.
- `timeout_seconds`: the manifest block, the capability definition, or
  `plugin.timeout_seconds`, clamped to 1–300 seconds.

Invocation calls `tools/call` with the resolved remote name. An MCP result is
normalized as follows:

- `structuredContent` (a JSON object) is returned under `data`.
- Otherwise text content items are joined and returned under `text`.
- `isError: true` raises an invocation failure with a readable message derived
  from the content; credentials are never echoed in the message.

### A2A agent card discovery

`discover_card(installation)` fetches the agent card at
`{plugin.endpoint_url origin}{agent_card_path}` and validates the minimum card
fields: `name` (non-empty string), `description` (string), `capabilities`
(object) and `skills` (list). A missing or malformed field raises a
configuration error naming the field.

The card's `capabilities.streaming` boolean is recorded on every discovered
capability. When `capabilities` is present but `streaming` is absent or not a
boolean, the card defaults to non-streaming (`false`); this relaxation is
intentional and covers older cards that omit the flag. A missing or
non-object `capabilities` block remains a required-field rejection.

Each skill is mapped to a capability:

- `name`: the skill `id` (falling back to `name`), normalized to the platform
  identifier pattern; the original is kept as `remote_name`.
- `description`: the skill description, truncated to 1,000 characters.
- `input_schema` / `output_schema`: the skill `inputSchema`/`outputSchema` (or
  `input`/`output`) converted to closed objects; a missing or non-object schema
  falls back to the empty closed object.

Invocation posts a JSON-RPC 2.0 `message/send` request to `plugin.endpoint_url`
with a single text part. The result is extracted from the first artifact text,
the first artifact part text, or the message parts; otherwise the raw result
object is returned under `data`.

Streaming is gated on the card's recorded flag. A caller may request streaming
through the reserved `stream: true` or `streaming: true` argument. When the
capability's recorded flag is `false` (including the relaxed default for a
missing or non-boolean card flag), such a request is rejected locally with
`CAPABILITY_STREAMING_UNSUPPORTED`; no `message/stream` or SSE request is sent
to the remote agent. When the card declares `streaming: true`, the call
proceeds over the existing `message/send` path. The reserved `stream` and
`streaming` flags are recognized by the adapter and are not tool-specific
arguments.

### OpenAI-compatible discovery

OpenAI-compatible endpoints expose no remote catalog surface. Capabilities are
declared in the manifest; the discover endpoint merges stored declarations
without a remote pull.

## Error codes

Router-level codes (owned by `core.agent_capability_router`) remain unchanged:
`CAPABILITY_NOT_DECLARED`, `CAPABILITY_NOT_INSTALLED`, `CAPABILITY_DISABLED`,
`CAPABILITY_PROTOCOL_UNSUPPORTED`, `CAPABILITY_AUTH_REQUIRED`,
`CAPABILITY_UNINSTALLED_IN_FLIGHT`, `CAPABILITY_INVOCATION_FAILED`.

Adapters define local stable codes in their own modules; the router code set is
not extended. Every adapter failure is recorded on the invocation record with
`error_code = CAPABILITY_INVOCATION_FAILED`; the local adapter code is carried on
the raised exception for direct callers and the discover endpoint.

### Shared argument validator

All four protocols (`http_json`, `mcp`, `a2a`, `openai_compatible`) validate
caller arguments through the same pure, data-driven validator in
`core.capability_argument_validation`, wired into the router's existing
"Capability arguments are invalid." branch before any adapter dispatch. The
bounded schema subset supports:

- `type`: `string`, `number`, `integer`, `boolean`, `object`, `array`,
  `null`; booleans are never accepted as `integer` or `number`.
- `properties`, `required`, and `additionalProperties: false` (unknown
  properties are rejected only when the schema closes the object).
- `enum`, array `items` / `minItems` / `maxItems`, string length bounds, and
  number `minimum` / `maximum`.
- Local `$ref` pointers resolved against `#/definitions/<name>` or
  `#/$defs/<name>`. External references, fragment-less pointers and
  unresolvable targets are rejected with
  `CAPABILITY_SCHEMA_REF_UNSUPPORTED` instead of being sent upstream.

Ordinary argument violations reuse the router `CAPABILITY_INVOCATION_FAILED`
token; no synonym argument code is introduced. A rejected argument never
reaches the remote endpoint, and the failure is recorded on the invocation
record with `status = FAILED` and `error_code = CAPABILITY_INVOCATION_FAILED`.

### HTTP JSON normalization

| Failure | Raised adapter code |
|---|---|
| Request timed out (read/write/pool timeout) | `CAPABILITY_TIMEOUT` |
| Connection refused/reset (`ConnectError`), connection timeout (`ConnectTimeout`), redirect loop (`TooManyRedirects`), rejected 3xx redirect | `CAPABILITY_UPSTREAM_UNREACHABLE` |
| Upstream 4xx status | `CAPABILITY_UPSTREAM_CLIENT_ERROR` |
| Upstream 5xx status | `CAPABILITY_UPSTREAM_SERVER_ERROR` |
| Non-JSON content type, malformed JSON, non-object body | `CAPABILITY_UPSTREAM_INVALID_RESPONSE` |
| Other non-success status, remaining transport failure, schema rejection | `CAPABILITY_INVOCATION_FAILED` |

### MCP normalization

| Failure | Raised adapter code | Notes |
|---|---|---|
| Remote application error (`isError: true`, JSON-RPC error frame) | `CAPABILITY_UPSTREAM_ERROR` | Readable message from content; no secret echo. |
| Request timed out (read/write/pool timeout) | `CAPABILITY_TIMEOUT` |
| Connection refused/reset (`ConnectError`), connection timeout (`ConnectTimeout`), redirect loop (`TooManyRedirects`), rejected 3xx redirect | `CAPABILITY_UPSTREAM_UNREACHABLE` |
| Upstream 4xx status | `CAPABILITY_UPSTREAM_CLIENT_ERROR` |
| Upstream 5xx status | `CAPABILITY_UPSTREAM_SERVER_ERROR` |
| Missing/unsupported content type, malformed JSON, malformed JSON-RPC frame | `CAPABILITY_UPSTREAM_INVALID_RESPONSE` |
| Other non-success status, remaining transport failure, schema rejection | `CAPABILITY_INVOCATION_FAILED` |
| Misconfigured plugin or manifest block | `CAPABILITY_CONFIG_INVALID` | Invalid tool mapping, missing required block. |

The shared HTTP and MCP connectors attach a `FAULT_*` taxonomy category to the
raised `ToolExecutionError`; the adapters map that category onto the unified
codes above. Connectors leave the original exception type and message intact,
and callers that do not inspect the category keep the previous generic
`CAPABILITY_INVOCATION_FAILED` behavior.

**Timeout vs. unreachable split:** a `ConnectTimeout` means the handshake never
completed, so it is classified `FAULT_UNREACHABLE` (mapped to
`CAPABILITY_UPSTREAM_UNREACHABLE`). Read, write and pool `TimeoutException`
subclasses mean a connected upstream stalled, and stay `FAULT_TIMEOUT` (mapped
to `CAPABILITY_TIMEOUT`). A connect timeout is never counted in both buckets.
The same split applies to the A2A and OpenAI-compatible adapters.

### OpenAI-compatible normalization

| Failure | Raised adapter code |
|---|---|
| Request timed out | `CAPABILITY_TIMEOUT` |
| Network-level transport failure or redirect | `CAPABILITY_UPSTREAM_UNREACHABLE` |
| Upstream 4xx status | `CAPABILITY_UPSTREAM_CLIENT_ERROR` |
| Upstream 5xx status | `CAPABILITY_UPSTREAM_SERVER_ERROR` |
| Non-JSON body, missing choices, non-string content | `CAPABILITY_UPSTREAM_INVALID_RESPONSE` |
| Missing model, invalid path, invalid endpoint | `CAPABILITY_CONFIG_INVALID` |
| `max_tokens` / `temperature` out of range or non-numeric | `CAPABILITY_ARGUMENTS_OUT_OF_RANGE` |
| Other adapter failure | `CAPABILITY_INVOCATION_FAILED` |

### A2A normalization

| Failure | Raised adapter code |
|---|---|
| Request timed out | `CAPABILITY_TIMEOUT` |
| Network-level transport failure or redirect | `CAPABILITY_UPSTREAM_UNREACHABLE` |
| JSON-RPC `error` frame | `CAPABILITY_UPSTREAM_ERROR` |
| Upstream 4xx status | `CAPABILITY_UPSTREAM_CLIENT_ERROR` |
| Upstream 5xx status | `CAPABILITY_UPSTREAM_SERVER_ERROR` |
| Card or response missing required shape | `CAPABILITY_UPSTREAM_INVALID_RESPONSE` |
| Card missing required field, invalid path, invalid endpoint | `CAPABILITY_CONFIG_INVALID` |
| Streaming requested by a card that does not declare streaming support | `CAPABILITY_STREAMING_UNSUPPORTED` |
| Other adapter failure | `CAPABILITY_INVOCATION_FAILED` |

## Security constraints

- **HTTPS only**: every endpoint is validated through the shared endpoint
  validator: HTTPS scheme, default port (443), no URL-embedded credentials, no
  fragment, no dot segments, and a public DNS hostname. IP literals, localhost,
  `.local`, `.internal`, `.test` hosts and non-public resolved addresses are
  rejected.
- **Allowlist**: the host must appear in `PLUGIN_HTTP_ALLOWED_HOSTS`; resolution
  fails closed otherwise.
- **Redirects forbidden**: transports never follow redirects.
- **Size caps**: response bodies are bounded to 1 MiB. The OpenAI-compatible
  request body is additionally bounded to 64 KiB; MCP request and response
  bodies share the connector's 1 MiB cap.
- **Credentials**: secrets are resolved from the bound credential reference
  through `core.plugin_credentials.resolve` (bearer, header or query contract).
  Secrets are injected as request headers (or a query pair) and are never
  logged, written to invocation records, audit metadata, or echoed in failure
  messages. Invocation records persist only installation, capability, protocol,
  status, error code and latency.
- **No vendor pinning**: no provider base URL, model family or account is
  hard-coded; every remote value comes from the stored plugin definition and
  manifest.
- **Closed schemas**: discovered capabilities are declared with closed JSON
  objects; arguments and results are validated against those schemas before
  dispatch and after the call.

## Manifest provenance and discovery cross-validation

A capability entry carries an optional `source` field that records where its
declaration came from:

- `"declared"` (default): the capability was written into the manifest by a
  administrator.
- `"discovered"`: the capability was produced by a remote discovery pull
  (MCP `tools/list` or an A2A agent card).

The discover endpoint tags every merged remote capability with
`"source": "discovered"` and writes a discovery record to the protocol
transport block:

```json
{
  "mcp": {
    "discovery": {
      "endpoint": "<sha256 of plugin.endpoint_url>",
      "discovered_at": "2026-11-22T10:00:00+00:00",
      "tool_count": 3,
      "tools": ["fetch-weather", "search-places", "book-trip"]
    }
  }
}
```

The same record shape lives under `manifest.a2a.discovery` for A2A plugins.
The `endpoint` field is the SHA-256 fingerprint of the current
`plugin.endpoint_url`; the record stores no secret and no raw URL credential.

### Validation rules

Validation runs both at plugin registration (catalog POST) and before the
discover endpoint persists a merge. A failure returns `400` and leaves no
write:

- `source` must be `"declared"` or `"discovered"`; any other value is rejected.
- A `"discovered"` capability is only valid for the `mcp` and `a2a` protocols.
  The OpenAI-compatible endpoint exposes no catalog pull, so its capabilities
  are declared in the manifest.
- For `mcp` and `a2a`, every capability (declared or discovered) must be
  covered by a discovery record for the current endpoint:
  - No discovery record exists → the entry is rejected with a message pointing
    to the discover endpoint first. A hand-written MCP or A2A capability cannot
    impersonate a remote tool.
  - The record's endpoint fingerprint does not match the current
    `plugin.endpoint_url` → the record is stale; the capability is rejected and
    discovery must be run again against the new endpoint.
  - The capability name is not listed in the record's `tools` → it is not
    covered by the latest discovery and is rejected.
- Changing `endpoint_url` invalidates the previous discovery record: stale
  discovered and declared capabilities fail registration validation until the
  discover endpoint runs against the new endpoint and refreshes the record.

The discover endpoint is idempotent: repeated pulls overwrite same-name
capabilities, preserve unrelated entries, never duplicate a capability, and
refresh `discovered_at`, `tool_count` and `tools` on each run.

## Discover endpoint

`POST /api/v1/tenants/<tenant_id>/agent-plugins/<plugin_id>/discover/` merges
remote-discovered capabilities into the plugin manifest.

- Tenant administrator permission is required; non-admin callers receive `403`.
- The protocol must have a registered discover adapter; unsupported protocols
  receive `400` and no write occurs.
- Discovery runs against an existing installation of the plugin when one
  exists; otherwise the call proceeds without a bound credential.
- Discovered capabilities are merged idempotently: same-name declarations are
  overwritten, unknown existing entries are preserved, and duplicates are not
  created. The merged manifest passes `agent_plugin_manifest.validate_manifest`
  before anything is persisted; a validation failure returns `400` without a
  write.
- Remote failures return `502` with a readable detail; unexpected exceptions
  are not swallowed. The response is
  `{"plugin_id", "protocol", "discovered", "capabilities"}`.
