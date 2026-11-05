# GUI parity and production-completion acceptance

## Authority and implementation rule

The reference is the complete `main` branch of [AI-Stack-Agent-GUI](https://github.com/Jasonzhu12397/AI-Stack-Agent-GUI), not only its landing page or navigation. The production application remains the Vue 3 + TypeScript frontend and Django REST API in this repository. Port user-visible workflows, states, validation, filters, dialogs, actions, and navigation; do not add a second React runtime.

A reference interaction is not complete merely because a similarly named Vue page exists. Every action must call a real API, persist authoritative state where appropriate, enforce tenant/project scope and server-side permissions, emit audit evidence for security-sensitive mutations, and have automated tests. Reference-only mock providers, in-memory stores, random map data, demo credentials, and simulated infrastructure state must not be carried into production.

## Full reference surface to port

- Global shell: header, tenant/project context, navigation, language selection, authenticated identity, health/empty/loading/error states, responsive layout.
- Dashboard: operational metrics, recent runs/activity, health indicators, invariant/security status, refresh and quick-run navigation/actions.
- Agent Studio: agent list/selection, create/edit, model and tool selection, configuration validation, save, immutable publish/versioning, run input, execution result/history, token/cost/status details.
- Workflow Studio: workflow list/selection, DAG visualization and editing, node/edge management, model/tool references, validation and execution layers, publish/versioning, run trigger and run history. Support every reference node type, including tool execution and human approval, with server-side validation and execution semantics.
- Governance: approval queue, risk/policy evaluation, approver eligibility, approve/reject with reason, immutable payload/hash binding, expiry and decision history. No client-only authorization.
- Integration Hub: connector catalog and categories, install/enable/disable/remove lifecycle, configuration and credential references, health checks, safe test invocation, permissions/scopes, version pinning, execution feedback. Implement the actual provider integrations represented by the reference rather than demo dispatchers.
- Built-in integration experiences: Google Maps search/map/POIs/routes; weather lookup and units/forecast; domestic Feishu, DingTalk, WeCom and Amap connectors. Use provider APIs and secret storage; never return secrets to the browser or fabricate provider responses.
- Metal3 Fleet: fleet overview, BareMetalHost inventory/details, EPH bootstrap lifecycle, target clusters, agent inventory/telemetry, command terminal/history, operations console, logs, alerts, filters, power/provisioning actions and safe recovery/reclaim lifecycle. Destructive actions require explicit policy gates and evidence; physical operations require hardware-backed integration tests before being marked supported.
- Audit & Activity: searchable/filterable event timeline, trace/resource/actor inspection, pagination and integrity verification; clearly distinguish security audit events from operational activity.
- Organization and members: tenant/project/member views, invitation, role/project assignment, revoke, refresh, quota/context display and server-enforced RBAC.
- Docs & Matrix: documentation navigation, capability/feature matrix, evidence-linked implementation/verification status and clear distinction between implemented, simulated, blocked and hardware-unverified capabilities.

## Production backend requirements

- PostgreSQL is the source of truth for tenant/project data, agent/workflow versions, runs, approvals, connector registrations/installations/connections, memberships, deployment plans, command records and audit events.
- Redis is transient cache/coordination only; durable execution and resumability must not depend on process memory or Redis-only state.
- Background execution must have bounded retries, idempotency, cancellation semantics, timeout handling, persisted step/result state and observable failure reasons.
- All API reads and writes must enforce tenant/project ownership and role/grant checks on the server. Secrets are references resolved server-side; they must not be stored in browser state, logs, audit payloads or ordinary API responses.
- External connector execution must enforce HTTPS/host allowlists, SSRF protections, timeouts, response/request limits, schema validation, safe redirects policy, credential isolation, per-call authorization and audit records.
- MCP requires real protocol transport, lifecycle, tool discovery, schema validation, permission checks and bounded execution; a registry/template alone is not an MCP implementation.
- Maps/weather/domestic integrations must have provider-specific adapters, configuration/credential handling, health checks, error handling and contract tests. A template or static sample response is not completion.
- Metal3/CAPI/Ironic operations must be version-aware, idempotent and fail closed. CAPI pivot, Ironic provisioning, Target Agent takeover, EPH reclaim/reimage and SR-IOV/DPDK/Multus dataplane verification are separate acceptance gates; do not represent read-only preflight or dry-run as a completed deployment.

## Verification gates

1. Reference inventory: every page, sub-view, control, modal, filter, state and action in the reference is mapped to a Vue route/component and a backend contract.
2. UI parity: Playwright tests cover navigation and each material interaction; screenshots are captured from the running application, not reconstructed mockups.
3. API: endpoint tests cover success, validation failures, unauthenticated access, cross-tenant/project access, role denial, concurrency/idempotency and pagination/filter bounds.
4. Persistence: PostgreSQL integration tests prove data survives API/worker restarts; migrations are clean and reversible where supported.
5. Runtime: worker and connector tests prove real dispatch, bounded failure behavior, audit emission and safe secret handling.
6. End-to-end: Compose smoke tests cover login, project selection, each core studio flow, and UI-to-API-to-database execution. Provider E2E tests use provider sandboxes or explicit opt-in credentials.
7. Infrastructure: isolated Metal3/Ironic lab tests prove actual BMH provisioning, CAPI pivot/postflight, Target Agent takeover, control-plane-first progression, worker enrollment and EPH reclaim. Hardware/network tests separately prove Multus/OVS/Calico/SR-IOV CNI and DPDK behavior on the target NIC/driver/firmware combination.
8. Release: no feature is labelled complete until its tests pass and its evidence is linked in Docs & Matrix. A green unit/build pipeline alone does not prove provider or physical hardware behavior.

## Completion reporting

Maintain a feature-by-feature matrix with: reference location, Vue implementation, API/worker implementation, persistence model, authorization/audit, automated tests, live integration evidence, and status. Use only these statuses: `NOT_STARTED`, `IN_PROGRESS`, `CODE_COMPLETE_TEST_PENDING`, `TESTED`, `EXTERNAL_CREDENTIALS_REQUIRED`, `HARDWARE_LAB_REQUIRED`, `BLOCKED`. Never infer completion from file existence or a passing unrelated CI job.

## Current known non-completion

This document is an acceptance contract, not a claim that parity is already achieved. Previously identified gaps include full visual DAG editing and all node types, complete provider-specific connector flows, OAuth/Vault/KMS credential brokering, MCP client/server runtime, full approval/idempotency semantics, complete Fleet operation workflows, real CAPI/Metal3/Ironic provisioning and pivot, Target Agent takeover, EPH reclaim, and physical SR-IOV/DPDK validation. Re-audit the current branch and update this list only with test-backed evidence.
