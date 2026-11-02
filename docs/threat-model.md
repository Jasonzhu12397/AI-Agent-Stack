# Threat model — initial baseline

## Assets

- Tenant/project definitions and user/role bindings.
- Agent and workflow drafts, published immutable versions and run snapshots.
- Tool inputs/outputs, connector configuration and credential references.
- Run state, approvals, traces, audit events and knowledge documents.
- Infrastructure inventory and, in later phases, controlled infrastructure actions.

## Trust boundaries

1. Browser to API: all client-provided IDs, tenant claims, parameters and serialized graphs are untrusted.
2. API to PostgreSQL/Redis/object storage: database is authoritative; cache entries must not bypass authorization.
3. Runtime to model provider: model output is untrusted and may be malformed or adversarial.
4. Runtime to tools/plugins/MCP/Skills: manifests, descriptions, resource text, scripts and outputs are untrusted.
5. Plugin executor to network: destinations, DNS answers, redirects and returned content are untrusted.
6. Control plane to external systems: credentials, target scopes, API/CRD versions and side effects require explicit verification.
7. Operator approval to execution: approval is valid only for the exact normalized plan, target, parameters, versions and policy state.

## Primary threats and required controls

| Threat | Required control |
|---|---|
| Cross-tenant object access | Server-derived Principal; tenant-scoped repositories and object-level authorization; default deny |
| Client-supplied tenant/project spoofing | Never authorize from request body tenant_id; derive scope from authenticated identity |
| Prompt/tool/Skill/MCP injection | Treat content as data; code-owned allowlist, schema, scope and policy checks |
| Secret disclosure | Store credential references only; redact logs/traces/errors; short-lived broker handles |
| SSRF and DNS rebinding | Resolve and validate all addresses; block loopback/private/link-local/metadata ranges; revalidate redirects and DNS |
| Duplicate external side effects | Idempotency keys, durable invocation records, explicit at-least-once semantics, no blind retry for non-idempotent writes |
| Stale or altered approval | Hash-bind normalized plan and target; one-time consumption; expiry and pre-execution authorization recheck |
| Worker crash or lease theft | DB transaction/row locking, lease owner/token, heartbeat and compare-and-swap terminal transitions |
| Malicious plugin/Skill | Isolated process/container, resource/network/filesystem restrictions, pinned versions and review status |
| Infrastructure misconfiguration | Read-only first; validate actual CRD/API schemas; no inferred fields; postcondition observation |

## Initial implementation boundary

The current scaffold has no authentication, tenant isolation, policy engine, credential broker, plugin sandbox, or durable task execution. These are explicit risks, not implied protections. Local fakes must never be mistaken for production security controls.

## Abuse cases for future regression tests

- User A substitutes User B's tenant/project/object ID.
- Tool result says to ignore policy and call a privileged tool.
- MCP server advertises a tool not granted to the current Principal.
- A plugin redirects a permitted hostname to a private or metadata IP.
- A retry occurs after a worker crash but before the external side effect is reconciled.
- An approval is reused after changing target, parameters, tool version or policy version.
- Secret values appear in exception text, prompt, trace, audit record or API response.
