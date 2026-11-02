# AI Agent Studio — Vibe Coding Execution Manual V2.0

Source: user-provided “AI Agent Studio：完整 Vibe Coding 执行手册 V2.0”. This is the engineering execution baseline, not evidence that the product or any external integration is complete.

## 1. Agent operating rules

Before every task card:
1. Inspect repository structure, dependencies, migrations, tests, CI and adjacent modules.
2. Report reuse points, gaps, risks and a bounded implementation plan.
3. Define request/response schemas, state transitions and failure modes before coding.
4. Add negative and positive tests; implement one vertical slice only.
5. Run relevant lint, type, unit, contract, integration and end-to-end checks. Record exact commands, environment, pass/fail/skip counts and evidence.
6. Update API contracts, migrations, UI-visible state, ADRs, threat model and verification matrix as applicable.
7. Do not claim a TODO, fake, static page, file presence or CI trigger is a completed product capability.

Model output, retrieved text, Skill instructions, MCP descriptions and plugin responses are untrusted data. Authorization is enforced by code, never by the model. Production writes, messages and infrastructure changes are forbidden unless a separate task explicitly authorizes a specific target and action.

## 2. Technology baseline and architecture

- API: Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic.
- UI: React, TypeScript, Vite; graph editor may use a mature library, but the serialized workflow format belongs to this platform.
- Persistence: PostgreSQL is authoritative for durable configuration, approvals, runs and audit indexes. S3-compatible storage holds originals/attachments. Vector storage is replaceable (pgvector may be evaluated). Redis is cache/rate-limit/coordination only, never the sole durable task store.
- Local: Docker Compose and deterministic FakeModel/FakeTool/FakeMCP fixtures.
- Production direction: Kubernetes, Helm/Kustomize, OIDC and external Secret Manager; not enabled by the local scaffold.
- Deployment shape: modular control plane + independent Agent/Workflow workers + isolated Plugin Executor. Keep module boundaries without premature microservice sprawl.
- External APIs, OAuth scopes, MCP SDK/transport, vendor fields and CRD schemas must be verified against authoritative sources and a target environment; unknowns go to `docs/verification-matrix.md`.

## 3. Non-negotiable invariants

- Effective tool authority = authenticated Principal grants ∩ Agent/Workflow allowlist ∩ connector authorization ∩ current policy, with resource-scope checks.
- Tenant/project scope is derived and enforced server-side; never trust client-supplied tenant_id.
- Drafts are editable; Published definitions are immutable snapshots. Each Run fixes Agent/Workflow, model, tool, Skill, knowledge and policy versions or immutable references.
- Writes require idempotency keys or explicit non-retryable semantics. Long-running task state is durable. Cancellation does not imply an external side effect was undone.
- Platform task state comes from PostgreSQL; external resource state comes from its source system. API object creation is not proof of successful deployment.
- Store credential references only. Secret values must not enter prompts, logs, traces, errors, snapshots or audit summaries.
- Terminal state changes use DB compare-and-swap or row locks. Leases/heartbeats/recovery prevent silent task loss. If exactly-once side effects cannot be guaranteed, document at-least-once and deduplication behavior. Never blindly retry non-idempotent writes.
- Approval binds a canonical hash of plan, target, critical parameters, published versions and policy version. Recheck authorization/tool status before execution; changed inputs invalidate approval.

## 4. Repository structure target

```text
apps/
  web-studio/          # Builder, runs, approvals, observability, admin
  platform-api/        # tenant, definitions, publish, connections, access APIs
  agent-worker/        # Agent Runs
  workflow-worker/     # Workflow Runs and Scheduler
  plugin-executor/     # isolated connector execution
  model-gateway/       # may initially be an internal package
packages/
  contracts/ domain/ runtime/ models/ policy/
  integrations/ knowledge/ observability/
connectors/ skills/ migrations/ deploy/ tests/ docs/
```

Existing repository names may be retained when boundaries are equivalent. Do not move or rewrite unrelated projects.

## 5. Phase cards and exit criteria

### P0 — engineering foundation
**P0-1 Technology decision and runnable skeleton:** API, independent Worker, React UI, PostgreSQL, migrations, Compose, OpenAPI, health checks, CI and tests. Add ADRs, verification matrix and threat model. Implement deterministic FakeModel, FakeTool and a local FakeMCP fixture. Document clean startup, migration and recovery. Test health, migration round-trip and invalid configuration. A fake MCP fixture is not real protocol compatibility.

Exit only when a clean environment starts API/UI/Worker, migrations and checks pass, and dependency versions/unknowns are recorded.

### P1 — identity, tenancy and durable execution
**P1-1 Organization and authorization:** Tenant, Project, User, RoleBinding, Quota, OIDC boundary and local-only development identity adapter. Default-deny object authorization for platform/org admin, app developer, connector admin, approver, user and auditor. Production startup must fail if development identity is enabled. Test cross-tenant/project access, role revocation, forged tenant IDs and pagination leakage.

**P1-2 Runs, audit, storage and safety:** Run/StepRun state machine; PostgreSQL atomic claims (SKIP LOCKED or equivalent), leases, heartbeat, expiry recovery, cancellation and dead-letter/failure marking; AuditEvent, trace_id, redaction, quota/rate-limit boundary. Test duplicate delivery, lease expiry, concurrent claims, terminal races, non-idempotent retry prevention, restart recovery and audit immutability.

Exit only when authorization matrix passes and killing/restarting Worker yields explicit recovery/failure without silent loss or uncontrolled duplicate side effects.

### P2 — models and Agent Runtime
**P2-1 Model Gateway:** ModelProvider/Config versions, credential references, capability flags, tenant grants; Chat/structured output/Embedding adapters; FakeModel and one opt-in OpenAI-compatible adapter. Add connection test, timeout, rate limit, safe retry/fallback, token/cost accounting and routing interface. Unknown prices remain unknown. Test provider failures, unsupported capability, credential non-disclosure, cross-tenant access and budget exhaustion.

**P2-2 Agent Builder/Runtime:** Draft editing for prompt, model, tool allowlist, output schema, context/memory policy, step/time/budget limits and optional knowledge reference. Publish immutable versions. Runtime loads RunSnapshot, obtains authorized Principal, gets structured intent from model, validates schema/allowlist/policy/target/budget in code, invokes tools and persists steps/results. Add debug, version compare, publish/rollback, invocation API and run detail/trace. Test loops, invalid args, hot tool disable, immutable publish, pinned run versions and injection not elevating privileges.

Exit: UI create/debug/publish -> async API run -> result/trace query; HTTP connection lifetime does not own execution.

### P3 — workflow engine and studio
**P3-1 Workflow Engine:** Versioned JSON DAG and static publish validation (entry, node IDs, variable references, schema, cycles, permissions and dependency versions). Add Start/Input, LLM, Agent, Tool, HTTP, Code, Condition/Switch, Loop/Iterator, Parallel/Join, Human Approval, Wait/Schedule, Knowledge Retrieval and End/Output incrementally. First slice: Start/LLM/Tool/Condition/End. Persist StepRun, input/output references, retry count, branches and Join conditions. Bound loops; sandbox Code. Support timeout, error branches, recovery, cancellation and compensation interface.

**P3-2 Studio/Scheduler/Webhook:** Visual graph editor with drag/drop, edges, schema forms, variable validation, error edges, version publish and replay; accessible form editor fallback. Persist next_fire_at/timezone; signed webhooks with replay protection, rate limit, input schema and tenant mapping. Manual/API triggers share execution path. Test dangling edges, DST, duplicate webhook, scheduler pause/resume and quotas.

Exit: publish and run workflows through UI; restart scheduler without duplicate non-deduplicable triggers; recovery resumes from persisted state.

### P4 — plugins and governance
**P4-1 Plugin SDK/Integration Hub:** Manifest includes identity/version/source/license/maintainer/tool schemas/scopes/risk/network targets/rate/idempotency/health/dependencies. Review, register, install, enable/disable, pin, grant, upgrade/rollback. Plugin SDK, Tool Router and isolated executor. Restrict CPU/memory/time/concurrency/filesystem/egress; no host socket or cluster-admin mount. HTTP/OpenAPI adapters defend against redirects, DNS rebinding, private/link-local/metadata SSRF. Test malicious manifests, schema mismatch, crash isolation, SSRF and tenant isolation.

**P4-2 Approval/Credential Broker/Policy:** Default-deny risk policy; controlled credential-reference resolution; approval request/expire/reject/approve/revoke; hash-bound one-time consume and pre-execution recheck; configurable dual approval. Test plan/target tampering, expiry, role revoke, replay, secret leak, emergency tool disable and post-approval authorization change.

Exit: FakeTool install/grant/test/call/audit/disable/rollback closes end-to-end; high-risk simulated writes never execute before approval.

### P5 — MCP, Skills and knowledge
**P5-1 MCP:** Select and verify SDK version; client connection/auth reference/capability negotiation/Tools-Resources-Prompts discovery/metadata cache/reconnect/validation/permission filtering/size-rate-time limits/trace-audit. Provide a restricted internal MCP server; expose no database or Secret. Test against local real protocol server for read-only and denial cases. Treat descriptions/resource text as untrusted.

**P5-2 Skills:** Manifest with docs/references/examples/optional scripts/source/license/maintainer/dependencies/review status; local/trusted repository import, indexing, search, version pin/rollback and Agent/Workflow binding. Scan scripts and run in sandbox without default network/Secret/host access. Skills cannot override policy or elevate permissions.

**P5-3 RAG and Memory:** Controlled upload/sync, parsing/OCR interface, dedupe/chunk/source page/title/ACL metadata, embeddings, hybrid retrieval and citations. Originals in object storage; vector adapter replaceable. Filter tenant/document ACL before retrieval; reconcile index updates/deletes and permission changes. Separate conversation history, short-term state and long-term memory; sensitive content is not auto-saved. Test revoked ACL, index residue, citation accuracy, injection, vector isolation and deletion propagation.

Exit: MCP has actual local protocol evidence; Skills cannot bypass grants; RAG upload->index->authorized retrieval->cited answer and revoked access/deletion tests pass.

### P6 — enterprise connectors
Implement read-only first: GitHub, Jira, Confluence, Microsoft Graph (mail/calendar/files), Teams, OneDrive/SharePoint, Amap/Baidu/Google Maps, weather, Prometheus/Grafana, DNS/IPAM. For each: tool schemas, OAuth/API-key reference, scope verification, pagination/rate/error handling, contract tests and admin authorization UI. Each write action is a separate task card and requires a specifically authorized test tenant. Do not automatically send messages, create issues, share links or change DNS/IPAM.

Exit: every “supported” connector has read success/denial/timeout contract tests and verified authorization scope. Writes are accepted individually with evidence.

### P7 — infrastructure and published applications
**P7-1 Kubernetes/CAPI/Metal3/Ironic/Redfish:** First inventory target K8s versions, CRD groups/versions/schemas, APIs, namespaces, service-account permissions and postconditions. Read-only Cluster/Machine/BareMetalHost/Events/Conditions first. Then only authorized test-environment plan/dry-run/risk/approval/controlled declarative writes/observation. API object accepted is not success; verify postconditions. Delete/erase/reinstall/power and production DNS/IPAM remain disabled without extra authorization, confirmation, recovery plan and rehearsal.

**P7-2 Published apps/collaboration/evaluation:** Publish Agent/Workflow as Web Chat, REST, Webhook and scheduled app with access scope, quota, rollback, sessions; test sets, regression evaluation, run comparison, usage/cost/latency, failure taxonomy and feedback; team draft permissions and publish gates.

Exit: only verified target environments can be called tested; published app access is bounded and audited.

### P8 — productionization and operations
OTel traces, structured logs, audit and metrics (API P95/P99, model tokens/cost/errors, Agent loops/budget, workflow queue/recovery, MCP/connectors, RAG ingestion/retrieval, infrastructure conditions). Helm/Kustomize, resources, probes, PDB, autoscaling, NetworkPolicy, dedicated ServiceAccounts, Secret injection, backup/restore, retention/deletion, upgrade/rollback. CI includes format/lint/type/unit/contract/integration/E2E/security/SBOM/image scan and ephemeral smoke. Fault-inject Worker restart, duplicate delivery, upstream timeout, DB outage, partial success and recovery; capacity/cost tests.

Exit only with test evidence, approved production change, restore rehearsal and operations handover. Untested HA/SLO are not marked satisfied.

## 6. API and data acceptance

Use `/api/v1`, JSON, UTC timestamps, pagination, Trace ID and unified errors:
`{error:{code,message,retryable,trace_id}}`.
Long tasks return 202 + run_id/status_url/trace_id. Writes support `Idempotency-Key`: same tenant/operation/key/body returns same result; same key with different body returns 409. Define retention and indexes.

Required API domains: identity/tenancy; Agent; Workflow; integration/connection/MCP/Skills; model/knowledge; approvals/audit; published apps/evaluation. Every list is tenant-filtered and stably paginated; every detail has object authorization; dangerous writes have plan binding, audit and explicit conflict/timeout/retry semantics.

## 7. Continuous security regression matrix

Test cross-tenant leakage across API/DB/cache/object/vector/audit; prompt/tool/Skill/MCP injection; credential leakage and rotation; SSRF including DNS rebinding and redirects; plugin supply chain and sandbox; duplicate delivery/idempotency; approval tampering/expiry/revocation; long-task recovery/cancel/partial success; RAG ACL revocation/deletion; infrastructure schema/version/permission mismatch.

## 8. Final acceptance

1. Tenant login, project/roles/model config and cross-tenant denial.
2. Agent create/debug/publish/API or Web Chat run; pinned versions, steps, token/cost, trace/audit; hot-disabled tool denied.
3. Workflow with condition/parallel/loop/wait/approval/error branch; kill Worker and recover without duplicate dangerous writes.
4. Read-only plugin/MCP/Skill; unauthorized and malicious outputs denied.
5. ACL document ingestion/retrieval with citations; revoke/delete prevents retrieval.
6. Connector simulator contract suite; real writes only with separate test authorization/evidence.
7. Kubernetes/CAPI/Metal3 read-only and approved test write-observe loop; otherwise simulation only.
8. Authorization, SSRF, injection, secret leakage, concurrency/recovery, backup/restore and rollback tests.

Final delivery labels each module as one of: `implemented`, `simulation-verified`, `isolated-environment-verified`, `production-verified`, `not-implemented`. Code presence is not acceptance.

## 9. ADR template

- ADR number/title:
- Context/goal:
- Alternatives/trade-offs:
- Decision/reason:
- Security/data retention/cost impact:
- Environment and test evidence:
- Rollback/owner:
- Unverified external dependencies:
