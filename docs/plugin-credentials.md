# Plugin Connector Credential Resolution

Plugin connectors that require authentication reference a server-side credential
through a URI stored on the connection record. The platform never receives a
raw secret at manifest or connection configuration time; the connector process
resolves the reference at invocation time through a pluggable provider and
fails closed when resolution does not succeed.

## Reference URI

A credential reference is a URI with a scheme that selects the provider:

```
env://PLUGIN_SECRET_GITHUB_TOKEN
```

- The scheme is lowercased and must match `[a-z][a-z0-9+.-]*`.
- The reference must be a non-empty string, contain no whitespace, and be at
  most 2048 characters.
- Only the URI is persisted; the resolved secret value is never stored,
  returned in an API response, or written to audit metadata.

## Resolver wiring

`settings.PLUGIN_CREDENTIAL_RESOLVER` holds a dotted Python path to a resolver
callable. The built-in default is `core.plugin_credentials.resolve`, which
dispatches to the registered provider registry. Setting the environment
variable `PLUGIN_CREDENTIAL_RESOLVER` overrides the default with a custom
callable; an empty string means "no resolver configured" and every
authenticated connection fails closed.

`settings.PLUGIN_CREDENTIAL_ENV_PREFIX` restricts which environment variables
the `env` provider may read. The default prefix is `PLUGIN_SECRET_`.

## Provider contract

A provider is a callable:

```python
Provider = Callable[[str], dict[str, str]]
```

It receives the full reference URI and must return a credential contract
dictionary with one of the following shapes:

| Type     | Contract                                             | Injection point            |
|----------|------------------------------------------------------|----------------------------|
| `bearer` | `{"type": "bearer", "token": "<token>"}`            | `Authorization: Bearer …`  |
| `header` | `{"type": "header", "name": "X-Api-Key", "value": "<secret>"}` | Request header |
| `query`  | `{"type": "query", "name": "key", "value": "<secret>"}` | Query parameter, only when the operation declares `auth_query_parameter` |

Providers must be idempotent: resolving the same reference twice returns an
equivalent dictionary. Providers raise a `CredentialError` subclass on any
failure; they must never return an empty or anonymous credential.

### Registry API

`core.plugin_credentials` exposes:

- `register_provider(scheme, provider)` — register or replace the provider for
  a scheme. Registering an existing scheme overwrites the previous provider.
- `unregister_provider(scheme)` — remove a provider registration.
- `providers()` — return a sorted tuple of registered scheme names for
  introspection.
- `redact(reference)` — return a masked form such as `env://***` for logs and
  user-facing messages.

### Built-in `env` provider

`env://VAR_NAME` reads the variable from the connector process environment.

- The variable name must start with the configured
  `PLUGIN_CREDENTIAL_ENV_PREFIX`; variables outside the prefix allowlist are
  rejected with `CREDENTIAL_NOT_FOUND`.
- A plain string value is returned as a bearer token.
- A JSON object value selects a header or query credential, for example:

```json
{"type": "header", "name": "X-Api-Key", "value": "…"}
```

- A JSON array, a non-object JSON value, or malformed JSON is rejected with
  `CREDENTIAL_INVALID_SHAPE`.

## Error codes

All resolver exceptions inherit `core.tool_router.ToolExecutionError` and carry
a `.code` attribute so existing call sites continue to handle them as tool
execution failures.

| Code | Condition |
|------|-----------|
| `CREDENTIAL_RESOLVER_NOT_CONFIGURED` | `PLUGIN_CREDENTIAL_RESOLVER` is empty; no resolver callable is wired. |
| `CREDENTIAL_REFERENCE_INVALID` | The reference is missing, blank, malformed, contains whitespace, or exceeds the length limit. |
| `CREDENTIAL_PROVIDER_UNSUPPORTED` | The URI scheme has no registered provider. |
| `CREDENTIAL_NOT_FOUND` | The provider could not locate the credential (variable missing, outside the prefix allowlist, or denied by the source). |
| `CREDENTIAL_INVALID_SHAPE` | The resolved value or its JSON encoding does not match a supported credential contract. |
| `CREDENTIAL_ACCESS_DENIED` | The configured resolver raised an unexpected internal error while resolving the reference. |

## Leak-prevention constraints

- Resolved secrets appear only on the outgoing request (header or query
  parameter). They are not written to the database, `AuditEvent` metadata,
  exception messages, or application logs.
- Persisted connection state stores only the reference URI; `redact()` masks
  everything after the scheme before a reference is logged or surfaced in a
  message.
- Exception messages never include the full reference, the variable name
  after the scheme, or any secret fragment.
- Audit metadata strips keys containing `token`, `secret`, `password`,
  `api_key`, `authorization`, `credential`, and similar sensitive parts.
- Tests assert that the secret is absent from `str(exc)`, captured log output,
  audit metadata, and the serialized tool result, and that the transport is
  never invoked when resolution fails.

## Fail-closed behavior

For a connector whose manifest marks authentication required, any resolver
failure aborts the operation before the HTTP client or transport is
constructed. The connector never falls back to an unauthenticated or anonymous
request.

## Future providers: Vault / KMS

External secret managers are expected to be added as additional registered
providers rather than changing the connector flow. A future implementation
would, for example, register a `vault` scheme:

- `vault://mount/path#key` resolves through a Vault or KMS client configured
  from platform settings;
- the provider reads a short-lived wrapping token from its own environment
  variables, never from the reference URI;
- access failures map to `CREDENTIAL_ACCESS_DENIED` or `CREDENTIAL_NOT_FOUND`;
- cached leases are renewed or discarded according to the provider TTL.

No Vault or KMS provider is implemented in this change.
