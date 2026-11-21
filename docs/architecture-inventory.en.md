# AI-Agent-Stack Architecture Inventory (based on the real code on `main`)

> Language: English · [中文](./architecture-inventory.zh-CN.md)

> Scope: this inventory is checked only against the current code of repository `Jasonzhu12397/AI-Agent-Stack` on the `main` branch. Design goals, documentation plans, and frontend templates are not counted as completed features.
> Inventory date: 2026-11-19; reviewed baseline HEAD: `d52e50bdbb8d53f31eb7b5d3f80b42c901dfa5ee`.

## 0. State definitions (shared vocabulary for this inventory)

| Label | Meaning | Criteria |
|---|---|---|
| 【Baseline】 Code baseline | An importable, test-covered implementation exists in the repo: a concrete directory, data model, view, execution function, or contract | Does not mean it has been proven to run in a real environment |
| 【Pending validation】 Implemented, pending validation | The code path is complete with unit tests or mock coverage, but lacks end-to-end acceptance in a real environment (real K8s / bare metal / real model provider / real vector store) | Failure and exception paths still need review against the real chain |
| 【To develop】 To be developed | Only a design, enum value, frontend template, or documentation plan exists; the runtime executor, adapter, or full UI interaction is not yet landed | Must not be advertised as supported |

## 1. Baseline evidence (this review)

- Local baseline (re-verified 2026-11-19): **975 passed**; `ruff check .` All checks passed; `manage.py check` 0 issues; `makemigrations --check --dry-run` reports no model drift.
- Test environment: no local PostgreSQL/Redis; tests run with an inline `DATABASE_URL=sqlite://` override; CI uses a PostgreSQL 16 service container.
- Scale: `apps/` + `packages/` Python source and tests total about 31.3k lines; 80 `tests/test_*.py` files.
- CI workflow: three jobs in `.github/workflows/ci.yml` — `python` (ruff / system check / migration drift / migrate / pytest), `web` (npm test / Vite build), `compose` (config validation of both compose files).
- History: the Python CI previously failed on three ruff import-ordering issues in `tests/test_worker_tasks.py` (not a test-logic failure); fixed in commit `5ba685b`. The re-run result of the remote CI was not re-checked this time.

## 2. Overall architecture overview (target form × real directory mapping)

Target form: **a general-purpose AI Agent platform + three infrastructure deployment Agents + the Metal3/Ironic/CAPI/Omni execution system**. AI Agents understand, plan, orchestrate, and call; the underlying infrastructure components perform the actual deployment. The platform itself is Multi-Agent; external AI Agents are plugged in through plugins/connectors and invoked by orchestration.

| Layer | Target components | Real code location | Current state |
|---|---|---|---|
| User entry | Web UI / Chat / Workflow / Agent management / plugin marketplace / operations console | `apps/web-studio/src/components/`, `src/services/api.ts` | 【Baseline】 Pages and API wiring exist; per-interaction acceptance still to be checked (see §6) |
| Platform backend | API Gateway / Auth / Agent Runtime / Workflow Engine / Model Router / Plugin·MCP Gateway / Knowledge·RAG / Token Budget / Audit·RBAC / Task Queue / Realtime Logs / Events | `apps/platform-api/core/`, `apps/worker/worker_app/` | 【Baseline】 Most modules implemented; realtime push, model routing/fallback, and the MCP stdio transport remain to be developed (see §3) |
| Infrastructure Agent control plane | Cloud Control / EPH / Target Agents | `apps/cloud_control_agent/`, `apps/eph_agent/`, `apps/target_agent/` | 【Baseline】 Independent entrypoints, protocols, and state machines exist for all three; the real bare-metal chain awaits validation (see §4) |
| Agent contracts & transport | Identity / command / lease / heartbeat / checkpoint / mTLS | `packages/agent_contracts/`, `packages/agent_transport/` | 【Baseline】 Contracts and transport implemented (see §7) |
| Execution layer | CAPI / Metal3 / Ironic·BMO / Omni / BMH Inventory / Kubeadm·K8s / Chrony·NTP / SR-IOV·DPDK·Multus | Skill/adapter modules inside the three Agents call `kubectl` / `clusterctl` / the Ironic HTTP client | 【Pending validation】 Execution adapters are mostly read-only observation plus fail-closed validation; real install/migration/data-plane forwarding awaits acceptance (see §5) |
| Physical bare metal | Servers / BMC·IPMI·Redfish / management network / control-plane network / data-plane network | `apps/platform-api/core/bmc_credentials.py`, fleet inventory views; BMC config persistence | 【Baseline】 Credential and inventory models exist; real BMC/Ironic power-on acceptance to be developed |

## 3. Platform layer: general-purpose AI Agent capabilities, item by item

| Module | Target responsibility | Code evidence | State | Gaps / notes |
|---|---|---|---|---|
| API Backend | Public API, users, projects, task management | DRF routes cover auth / tenant / project / task / model-provider / model-config / agent / agent-run / workflow / run / knowledge / connector / installation / connection / cluster-deployment / fleet / agent-node / command / approval / audit / member / token-budget / sriov / handoff / postflight (`core/urls.py`) | 【Baseline】 | Error states, pagination, and permission boundaries still to be checked per endpoint |
| Agent Runtime | Agent lifecycle, context, tool calls | `apps/worker/worker_app/tasks.py`: DB task records, atomic claim, lease token/expiry, heartbeat renewal, bounded retries, cooperative cancel; `core/agent_views.py`: Agent definition / version / publish / run records | 【Baseline】 | Context retention and failure recovery across multi-step tool calls still to be reviewed against the real model chain |
| Workflow Engine | Multi-step orchestration, state transitions | `core/workflow_engine.py`: node types `start / llm / agent / condition / knowledge_search / tool / approval / loop / end`; `core/workflow_views.py`: validate / publish / runs | 【Baseline】 | 【Baseline】 Parallel `any/all` joins, `approval` suspend/resume and bounded checkpointed `loop` nodes implemented; remaining work is visual editing |
| Model Router | Unified access and routing for domestic and foreign models | `core/model_gateway.py`: `ProviderType` is only `fake` and `openai_compatible`; chat `/chat/completions`, embeddings `/embeddings` over HTTP; errors marked retryable on 5xx/429 | 【Baseline】 | 【To develop】 Multi-provider primary/standby routing, fallback chain, degradation; domestic providers are reached via OpenAI-compatible endpoints, no native SDK; FAKE is test-only |
| Plugin / MCP Gateway | Plugins, MCP Server, third-party system integration | `core/plugin_views.py`: connector catalog, approval, project install, explicit grant, connection records; `core/http_connector.py`: `register_connector_tool` dispatches by connector type (`http` / `openapi` / `mcp`) and keeps `register_http_tool` as a compatibility alias; `core/mcp_connector.py`: JSON-RPC client for the streamable HTTP transport (initialize / tools/list / tools/call, SSE response parsing, protocol-version negotiation, tool mapping, 1 MiB response ceiling, redirect and host-allowlist rejection); `core/mcp_tool_sync.py` + `core/mcp_views.py`: `POST /api/v1/plugin-connections/<id>/sync-tools/` discovers and imports remote tools (new tools land with `enabled=False` pending review, capped at 256 per connection) | 【Baseline】 (HTTP / OpenAPI / MCP channels) | 【Pending validation】 Real MCP server integration and the human approval loop after tool import; 【To develop】 stdio transport, OAuth authorization-code flow and token refresh (credentials currently resolve only bearer / env references), and a server mode where the platform itself is exposed as an MCP server |
| Knowledge / RAG | Documents, vector retrieval, knowledge bases | `core/knowledge_models.py`, `core/knowledge_views.py`, `core/rag_runtime.py`, `core/vector_store.py`: KB/document/embedding-model binding; `QdrantVectorStore` (urllib HTTP, needs `QDRANT_URL` / collection / api-key env references); collections versioned per embedding model and dimension (`collection_for_model` / `for_model`, e.g. `agent_knowledge__bge-m3-1024`); first ingest creates the collection idempotently (`ensure_collection`); ingest writes back `vector_collection` / `vector_dimensions`, and delete/retry clean up that exact collection; `core/management/commands/reindex_knowledge.py` re-embeds documents with the current model (skips documents already in the target collection by default; `--force`, `--dry-run`, `--document` for targeted retries, and per-document failure isolation); local cosine similarity | 【Baseline】 | 【Pending validation】 Full ingest/retrieve chain against a real Qdrant instance + embedding model; 【To develop】 Cleanup / capacity reclamation for legacy collections (history is retained and never deleted automatically), pgvector and S3-compatible object storage (see §7.3) |
| Token Budget | Budget reservation, usage settlement, overspend protection | `core/token_budget.py`, `core/token_budget_views.py`: tenant-level reservation, settlement, idempotency, overspend protection; includes regression tests | 【Pending validation】 | Local tests are green after several rounds of fixes; settlement consistency still to be reviewed under concurrent workers and real provider usage reporting |
| Task Queue / Scheduler | Cache, task queue, async execution | `apps/worker/worker_app/main.py` polls `process_once`; task claim and lease are based on PostgreSQL records; `redis` is in dependencies, `settings.py` only defines `REDIS_URL` | 【Baseline】 (DB queue) | 【To develop】 Redis actually used as cache / queue / coordination channel: no `CACHES`, channel_layer, or redis client call found in code; queue failure recovery, backlog, and replay still to be validated |
| Audit / RBAC | Permissions, tenant isolation, operation records | `core/membership_views.py`: tenant/project members and roles (admin/editor/viewer); views scoped by `memberships__user`, non-admins filtered again by project membership; `core/audit.py`: two chained-hash chains (`verify_audit_chain` / `verify_global_audit_chain`), with a nullable `tenant` so tenant-less authentication events can be recorded (migration `0024_audit_event_nullable_tenant`); `core/auth_views.py` writes login success / login failure / logout into the global chain; persistence covers the agent-node / agent-plugin / cluster-deployment / membership / plugin / tool-approval / model / knowledge / agent / workflow / task / token-budget / tenant / project write endpoints plus authentication events | 【Baseline】 | Platform-side write endpoints and authentication events are both chained; the global chain already exposes read-only list / verify (`GET /api/v1/audit-events/global/` and `/verify/`, platform administrators only); 【To develop】 the Audit page does not surface the global chain yet; policy loop to be fully covered |
| Realtime Logs / Events | Realtime logs for Agents, Workflow, deployments | Frontend `api.ts` pulls primarily via requests; no channels consumer / `group_send` / channel_layer implementation found in code | 【To develop】 | Realtime push channel (WebSocket/SSE) not landed yet; current experience is polling |
| Multi-Agent onboarding | External AI Agents plugged in as plugins to collaborate | Current onboarding shape: third-party capabilities packaged as connector tools (HTTP / OpenAPI schema / remote MCP tools), invoked by Agent/Workflow after tool-router permission/approval; MCP tools can be discovered via `sync-tools` and then enabled by a human reviewer | 【Baseline】 (tool-level onboarding, including MCP tool import) | 【To develop】 Full onboarding protocol that treats an external AI Agent as an orchestratable node returning structured results and participating in state transitions; session-level MCP Agent onboarding (A2A-style protocols) still to be designed |

### 3.1 Planned plugin and model integrations (current real state)

| Category | Planned items | Code state | State |
|---|---|---|---|
| Maps | Amap, Baidu Maps, Google Maps | Frontend `src/domain/connectorTemplates.ts` holds HTTP template metadata for amap places/geocoding/routing, baidu places/geocoding, google places | 【Baseline】 (template/HTTP contract declaration); real connection, auth, and backend-call acceptance pending validation |
| Weather | Weather API / weather service | Template includes open-meteo (HTTP, no key) | 【Baseline】 (template); real connector pending acceptance |
| Dev collaboration | GitHub, Jira | Template includes `github-readonly` (HTTP); no Jira template or dedicated executor | GitHub read-only 【Baseline/Pending validation】; Jira 【To develop】 |
| Office collaboration | Microsoft 365, Teams | Template includes `microsoft-graph-readonly` (HTTP); no dedicated Teams connector | 【Baseline】 (read-only template); full Graph scopes, OAuth, and Teams integration 【To develop】 |
| Extensibility | MCP, third-party APIs, custom plugins | Connector catalog/install/grant/connection-test framework exists; the executor supports HTTP / OpenAPI / MCP (streamable HTTP plus tool discovery and import) | Framework 【Baseline】; MCP runtime 【Baseline/Pending validation】 (stdio and OAuth 【To develop】); isolated custom-plugin execution 【To develop】 |
| Models | Domestic and foreign providers, OpenAI-compatible, routing and fallback | Gateway is only `openai_compatible` + `fake`; the OpenAI-compatible channel can be reused to reach any compatible endpoint | 【Baseline】 (compatible channel); provider coverage, explicit routing, primary/standby fallback 【To develop】 |

> The connector templates above are product planning and metadata declarations; they do not mean the corresponding OAuth flow, real provider integration, or production-grade credential custody are complete.

## 4. Infrastructure deployment layer: actual division of the three Agents

### 4.1 Code facts (authoritative, used to align documentation)

| Agent | Directory | Implemented actions / modules | State | Gaps |
|---|---|---|---|---|
| Cloud Control Agent | `apps/cloud_control_agent/` (`api.py`, `main.py`, `deployment_controller.py`) | HTTP control plane: action/role validity check, reported capabilities check, checkpoint actor transition check, certificate CN identity check, deployment status aggregation and DB persistence; result-driven auto-advance queues the next planned step after a successful checkpointed result, deduplicates by phase-scoped idempotency key, and retries pending progression when an Agent registers | 【Baseline】 | 【To develop】 Real cloud-provider (Alibaba Cloud / Tencent Cloud / AWS) VM orchestration adapter: no cloud SDK found in `pyproject.toml` or code; this is currently a control-plane protocol and validation layer, not a cloud resource orchestrator |
| EPH Agent | `apps/eph_agent/` (`main.py`, `bootstrap_renderer.py`, `control_stack.py`, `control_stack_adapter.py`, `metal3_skill.py`, `capi_renderer.py`, `ntp_policy.py`, `templates/`) | Actions: `eph.preflight`, `eph.source.observe`, `eph.bootstrap.render` (pure kubeadm init / NoCloud seed / Agent Deployment rendering with artifact digests and no cluster writes), `eph.controlstack.apply` (idempotent cert-manager→CAPI→BMO→Ironic→CAPM3 apply plus readiness probes), `eph.metal3.bmh.apply`/`observe`/`wait_ready`, `eph.capi.cluster.apply`/`observe`/`wait_ready`; reported capabilities are generated from the single source in `packages/agent_contracts/orchestration.py` | 【Baseline】 | 【Pending validation】 Single-node Bootstrap cluster and target Control Plane bootstrapping to be accepted on real CAPI/Metal3/Ironic versions; there is **no** pivot adapter under the EPH directory (see conflict list §8) |
| Target Agent | `apps/target_agent/` (`main.py`, `capi_pivot.py`, `handoff.py`, `metal3_ownership.py`, `network_plugins.py`, `network_stack_adapter.py`, `cleanup_scope.py`, `eph_cleanup.py`, `worker_provisioning.py`, `postflight.py`, `ntp_attestation.py`, `ntp_policy.py`, `ironic_client.py`, `leader_election.py`, `worker_attestation.py`) | Actions: `target.cluster.observe`, `target.capi.observe`, `target.capi.pivot` (with dry-run), `target.metal3.observe`/`verify_ownership`, `target.worker.ntp.validate`, `target.ntp.control_plane.render`/`verify_bootstrap`, `target.ntp.internal.probe`, `target.network.apply` (idempotent Calico/Multus/OVS/SR-IOV/DPDK apply plus readiness observation), `target.handoff.evaluate`, `target.eph.cleanup`, `target.worker.provision`; pivot adapter, autonomous handoff state machine, BMH identity-set and ownership validation, EPH cleanup scope, worker provisioning, NTP attestation, three-replica Leader Election, Ironic HTTP client | 【Baseline】 | 【Pending validation】 Autonomous handoff once the real cluster is ready, CAPI Pivot, BMH migration, EPH cleanup, physical worker deployment and hardware acceptance; currently mostly fail-closed prechecks plus unit/mock coverage, the real provider matrix has not been run through |
| Platform Worker | `apps/worker/worker_app/` | Async execution of general AI Agent tasks and Workflows (model calls, tool routing, RAG retrieval, workflow runs) | 【Baseline】 | Boundary: it only handles platform-side general orchestration execution and does not replace the three infrastructure Agents above |

### 4.2 Locked responsibility boundary (single source of truth; code follows this)

| Role | Final responsibility | Does not own |
|---|---|---|
| Cloud Control Agent | Receive tasks, global orchestration, authorization, budget, dispatch, status aggregation, failure coordination | Does not approve the Target takeover; does not perform bare-metal changes directly |
| EPH Agent | Single-node Bootstrap, source-cluster precheck, bootstrap the target Control Plane | Does not perform the CAPI Pivot or the Target takeover |
| Target Agent | Autonomous handoff once the target cluster is ready, CAPI Pivot, BMH migration, EPH cleanup, worker deployment and final validation | Does not need Cloud Control approval for the takeover action |
| Platform Worker | Async execution of general AI Agents and Workflows | Does not replace infrastructure Agents operating clusters/bare metal |

## 5. Deployment lifecycle state machine and network / NTP status

### 5.1 Seven-step lifecycle (core business state machine)

Implementation: the platform creates a Cloud Control deployment and dispatches it through `core/cloud_control_bridge.py`; phases, commands, retries and checkpoint binding live in `apps/cloud_control_agent` and `packages/agent_contracts/orchestration.py`. The legacy `core/cluster_deployment_services.py` chain and its Target handoff/checkpoint APIs are removed.

| Step | Target flow | Code state | State |
|---|---|---|---|
| 1. Task creation and resource check | Cloud Control receives the request and checks machines, BMC, network, images, parameters | Deployment record creation, node/BMC/parameter validation, compatibility determination | 【Baseline】 |
| 2. Deploy the single-node EPH K8s | One unique Bootstrap cluster, not one EPH per machine | EPH preflight / source observe / CAPI cluster apply | 【Pending validation】 |
| 3. EPH bootstraps the target CP | Deploy the target K8s Master with CAPI/Metal3/Ironic | EPH CAPI cluster observe, template rendering; provider version compatibility precheck | 【Pending validation】 |
| 4. Target Agent takeover | Autonomous takeover once CP is Ready, no Cloud approval needed | Target handoff evaluation, cluster identity/controller readiness validation, checkpoint CAS | 【Pending validation】 |
| 5. Clean up EPH and migrate BMH ownership | Clean the Bootstrap, handle BMH inventory, ownership, resource attribution | `metal3_ownership.py`, `cleanup_scope.py`, `eph_cleanup.py`: identity-set and scope validation | 【Pending validation】 |
| 6. Deploy target workers | Target drives worker deployment and validates node status | `worker_provisioning.py`, `worker_attestation.py`, `postflight.py` | 【Pending validation】 |
| 7. Final acceptance | Nodes Ready, CNI, SR-IOV, DPDK, NTP, Ironic, cluster network | Postflight readiness determination and evidence collection; no real acceptance of DPDK/data-plane forwarding yet | 【Pending validation】; data-plane part 【To develop】 |

### 5.2 Network layering

| Plane | Target components | Code state | State |
|---|---|---|---|
| Control Plane (standard NIC) | API Server / etcd / management interface / Ironic internal services | Deployment planning and NTP source policy distinguish control plane / data plane | 【Baseline】 |
| Data Plane (SR-IOV/DPDK) | Pod data traffic, VFs, multi-NIC | SR-IOV Device Plugin config validation contract (`packages/agent_contracts/sriov_device_plugin.py`), view persistence, postflight resource-name check; Calico/Multus/DPDK appear as compatibility-determination fields (read-only validation) | SR-IOV config 【Baseline/Pending validation】; real install/upgrade/rollback of OVS/Calico/Multus/DPDK and real data-plane traffic validation 【To develop】 |

### 5.3 NTP rules

- Control Plane: uses external NTP.
- Worker: chronyd must use the Ironic internal IP/VIP of the target K8s Control Plane as the time sync source; external IP pools and fallback are forbidden.
- Code state: `eph_agent/ntp_policy.py`, `target_agent/ntp_policy.py`, `target_agent/ntp_attestation.py` implement policy validation and internal-source probing; generate chrony host commands / templates.
- State: policy validation and command generation 【Baseline】; the generated host commands are not yet injected into CAPI bootstrap templates or executed on physical hosts, so real NTP sync acceptance is 【Pending validation/To develop】.

## 6. Frontend GUI inventory (`apps/web-studio/src/components/`)

Acceptance bar: every button connects to a real API, can persist data, execute the action, and handle failure states correctly; static display alone does not count as complete.

| Page | Component | Code state | State | Gaps |
|---|---|---|---|---|
| Login | `LoginView.vue` | Calls `/api/v1/auth/csrf\|login\|logout\|me` | 【Baseline】 | Full auth experience, error states, session recovery to be checked |
| Dashboard | `DashboardView.vue` | Pulls real tasks / nodes pagination; computed derives active/succeeded/online metrics | 【Baseline】 | Metrics come from task/node tables, not independent monitoring signals; trends and realtime refresh to be added |
| Agent Studio | `AgentStudioView.vue` | Calls agents / publish / runs / model test | 【Baseline】 | Multi-step tool-call progress display and failure recovery to be improved |
| Workflow Studio | `WorkflowStudioView.vue` | Visual DAG canvas with layer layout, draggable nodes, node inspector, edge editor and JSON fallback; approve/reject actions | 【Implemented】 (visual editing/validation/approval/loop) | 【To develop】 Drag-to-connect edges, persisted node coordinates |
| Model Manager | `ModelManagerView.vue` | Calls model-providers / models / model-configs test | 【Baseline】 | Provider coverage, routing and fallback strategy UI to be added |
| Integration Hub | `IntegrationHubView.vue` | Calls connectors / installations / connections / test / status | 【Baseline】 | Real connectors, OAuth flow, and connection health loop to be completed |
| Knowledge Base | `KnowledgeBaseView.vue` | Calls knowledge-bases / documents / retry | 【Baseline】 | Full RAG chain (embedding/retrieval/recall quality) to be checked |
| Infrastructure Agents | `InfrastructureAgentsView.vue` | Calls agent-nodes / commands / approve | 【Baseline】 | Real deployment loop, phase evidence, and failure states pending validation |
| Metal3 Fleet | `Metal3FleetView.vue` | Calls fleet machines / BMC config / lifecycle-review | 【Baseline】 | Physical device operations and hardware acceptance pending validation |
| Governance | `GovernanceView.vue` | Calls tool-approvals / approve / reject | 【Baseline】 | Full policy loop (approval rules, override interception) to be completed |
| Audit | `AuditView.vue` | Calls audit-events / verify | 【Baseline】 | Platform-side write endpoints and authentication events are both chained; the global chain (authentication events) already has a read-only query and verify API; 【To develop】 the frontend view does not surface it yet |
| Organization | `OrganizationView.vue` | Calls tenants / projects / members | 【Baseline】 | Invitation flow, role-change notifications, etc. still to be improved |
| Docs & Matrix | `DocsAndMatrixView.vue` | Static phase matrix + external verification-matrix links; no API calls | 【Baseline】 (documentation display) | Matrix status must be driven by real acceptance evidence and kept up to date |
| SR-IOV Editor | `SriovDevicePluginEditor.vue` | Child component referenced by the deployment view; does not import api.ts directly | 【Baseline】 | End-to-end check of config to backend persistence to be verified |
| Workspace | `WorkspaceView.vue` | Workspace container / navigation | 【Baseline】 | — |

> 15 `.vue` components in total; 13 import `services/api.ts` directly. `DocsAndMatrixView` is a static matrix page; `SriovDevicePluginEditor` is an editing child component reused by a parent view.

## 7. Backend services and data architecture

### 7.1 Service catalog

| Service | Directory | Role | State |
|---|---|---|---|
| Platform API | `apps/platform-api` | Django REST API, authentication, permissions, data management | 【Baseline】 |
| Worker | `apps/worker/worker_app` | Async tasks, general Agent execution, Workflow execution (`tasks.py` / `main.py`) | 【Baseline】 |
| Cloud Control Agent | `apps/cloud_control_agent` | Cloud deployment control plane (protocol validation / status aggregation) | 【Baseline】; real cloud orchestration 【To develop】 |
| EPH Agent | `apps/eph_agent` | Bootstrap cluster operations and source-side observation | 【Baseline/Pending validation】 |
| Target Agent | `apps/target_agent` | Target cluster takeover, pivot, cleanup, worker deployment | 【Baseline/Pending validation】 |
| Agent Contracts | `packages/agent_contracts` | Agent identity/command/lease/heartbeat/checkpoint contracts; SR-IOV config validation contract | 【Baseline】 |
| Agent Transport | `packages/agent_transport` | mTLS communication, lease renewal, transport client | 【Baseline】 |

Note: `worker_app/capi_pivot.py` is a reserved/leftover module not referenced by `tasks.py` / `main.py` / tests and does not participate in the execution chain; the production pivot adapter is `apps/target_agent/capi_pivot.py`.

### 7.2 Deployment manifests

- `deploy/agents/`: K8s manifests for the three Agents (cloud-control / eph / target-cluster-agent), namespaces, build and deploy scripts.
- `deploy/cloud-control-plane/agent-mtls/`: haproxy / nginx mTLS config and deployment docs.
- `deploy/docker-compose.yml`, `deploy/docker-compose.cloud.yml`: local and cloud compose configs (CI runs `config` validation only).

### 7.3 Data layer status

| Store | Role | Code state | State |
|---|---|---|---|
| PostgreSQL | Business data, tasks, Agent state, deployment records, checkpoints | `DATABASES` configured via `dj-database-url`; business models and checkpoint persistence landed | 【Baseline】 |
| Redis | Cache and coordination; must not be the sole persistence source | `settings.py` only defines `REDIS_URL`; no actual cache/queue/channel call found in business code | 【To develop】 (wire it in and validate failure recovery) |
| pgvector | Planned vector retrieval store | Code does not use pgvector; the current vector integration talks to the Qdrant HTTP API | 【To develop】 (planned item; do not record as already wired) |
| S3-compatible | Planned document/object storage | No boto3 / S3 client found in code | 【To develop】 (planned item) |
| Secret Manager / Vault | Credential custody, rotation, authorization resolution | Credential reference resolution (env reference / bearer), BMC credentials, Agent certificate issuance and rotation endpoint; positive-number validation for `BMC_VAULT_TIMEOUT_SECONDS` | 【Baseline】; full production-grade key custody, automatic rotation, fine-grained authorization resolution 【To complete】 |

## 8. Documentation–code responsibility conflict list (to align to §4.2)

**Code facts**: the CAPI pivot adapter lives in `apps/target_agent/capi_pivot.py`, wired by line 25 of `apps/target_agent/main.py` via `from .capi_pivot import execute_capi_pivot`; Cloud Control dispatches actions `target.capi.pivot.dry_run` / `target.capi.pivot`; `capi_pivot.py` does not exist under `apps/eph_agent/`.

> **Status (aligned 2026-11-18)**: the old wording listed below has been reconciled to the §4.2 responsibility boundary in this documentation pass; the "Conflicting wording" column retains the previous wording for traceability only, and the "Should be" column states the current wording.

The following documents previously followed the old wording that "the EPH performs the source-side Pivot", which did not match the code; they have now been reconciled:

| File | Location | Conflicting wording | Should be |
|---|---|---|---|
| `README.md` | "Authoritative three-Agent" item 2 | EPH Agent owns source-side CAPI Pivot operation | EPH only does Bootstrap / source precheck / bootstrap the target CP; Pivot belongs to the Target Agent |
| `README.md` | "Not yet production-complete" section | "a new EPH CAPI Pivot adapter" | The pivot adapter is in the Target Agent; there is no pivot adapter on the EPH side |
| `docs/adr/0004-separate-cloud-and-node-agent-runtimes.md` | Line 17 | EPH owns source-side CAPI Pivot operation | Same as above; align to Target ownership |
| `docs/adr/0005-three-agent-fresh-runtime.md` | Line 17 | EPH owns … source-side CAPI Pivot operation | Same as above; note lines 36 and 40 of the same file already use the Target wording, so the document is internally contradictory and needs alignment |
| `docs/capi-pivot-contract.md` | Title, lines 3, 7, 8 | "EPH CAPI Pivot contract"; claims implementation by `apps/eph_agent/capi_pivot.py`, actions `eph.capi.pivot.dry_run` / `eph.capi.pivot` | Adapter path should be `apps/target_agent/capi_pivot.py`; actions should be `target.capi.pivot(.dry_run)` |
| `docs/dual-agent-infrastructure-architecture.md` | Line 8 | EPH performs … approved source-side CAPI Pivot operation | Same as above; align to Target ownership |
| `docs/eph-node-lifecycle.md` | Lines 42, 44, 68 | EPH Pivot adapter runs `clusterctl move`; references `apps/eph_agent/capi_pivot.py` | The pivot step belongs to the Target Agent; EPH keeps only Bootstrap / pre-cleanup observation |
| `docs/agent-command-api.md` | Line 25 | "The Node Agent runs `clusterctl move --dry-run`" (Node Agent retired) | The dry-run simulation and read-only checks belong to the Target Agent; the Node Agent wording is removed |

> Revision principle: code and new documentation follow the §4.2 responsibility boundary; historical ADRs that retain the old decision should be marked superseded so old and new wording do not coexist.

## 9. Capabilities already in place vs. not yet production-complete

### 9.1 Implemented in code (with unit/mock coverage; this does not mean real-environment acceptance passed)

- Independent runtime, Dockerfile, and deployment manifests for the three Agents.
- Agent identity, command, lease, heartbeat, checkpoint, and result protocol (contracts + transport).
- Agent mTLS communication; certificate identity bound to the registered Agent UUID.
- Target Agent multi-replica deployment, pod anti-affinity, PDB, Kubernetes Lease Leader Election.
- CAPI pivot prechecks and security gates (cluster identity, controller readiness, version compatibility, BMH identity set, approval digest binding).
- BMH inventory, ownership, and EPH cleanup-scope validation.
- Worker provisioning and internal Ironic NTP address validation / attestation.
- SR-IOV Device Plugin config validation and resource-matching check.
- Token budget reservation, settlement, idempotency, and overspend protection.
- Agent, Workflow, plugin, knowledge-base related APIs and frontend page baseline.
- Tenant/project member isolation, role validation, chained-hash audit records and verify (tenant chain + global authentication chain).
- The MCP client runtime (streamable HTTP JSON-RPC, tool mapping, SSE responses, timeouts and allowlist gates) and the tool discovery/import endpoint.
- Qdrant collection bootstrap (idempotent create), the readiness probe, and collections bound and recorded per embedding model / dimension.
- `manage.py reindex_knowledge`: re-embeds historical documents with the current embedding model and writes back the collection and dimensions, with dry-run, targeted retries, and per-document failure isolation.
- Read-only global audit chain endpoints: `GET /api/v1/audit-events/global/` (paginated list) and `GET /api/v1/audit-events/global/verify/` (chain verification), readable by platform administrators only (`is_staff` / `is_superuser`).

### 9.2 Not yet production-complete (needs further development or real-environment acceptance)

- The MCP stdio transport, server mode, and real MCP server integration (the streamable HTTP client and tool discovery are already in place).
- Full third-party connectors and OAuth credential authorization, token refresh flow.
- A cleanup and capacity-reclamation strategy for legacy Qdrant collections (`reindex_knowledge` already re-embeds, and legacy collections are never deleted automatically); surfacing the global audit chain (authentication events) in the frontend.
- Advanced Workflow nodes: drag-and-drop canvas, parallel, loop, human-approval resume.
- Isolated plugin execution and full provider integration acceptance with SSRF protection.
- Full GUI parity with the reference repository, page by page and interaction by interaction (buttons on real APIs, persistence, failure states).
- Network plugins (Multus / SR-IOV CNI·Device Plugin / OVS / Calico / DPDK) full install, upgrade, and rollback.
- Full CAPI / Metal3 / Ironic deployment, pivot, and BMH takeover in a real environment.
- Real EPH cleanup, physical worker deployment, and hardware acceptance.
- Real SR-IOV / DPDK data-plane traffic validation.
- Full cluster upgrade, scale-out, scale-in, repair, and Day-2 operations.
- Redis cache/queue/coordination channel landing and failure recovery; pgvector and S3 object storage integration.
- Multi-provider model routing, primary/standby fallback, and native adaptation to domestic/foreign providers.

## 10. Suggested cleanup and completion order

1. **Align the wording first**: revise the conflicting docs and README per §8, fixing pivot responsibility to the Target Agent; fill the status in `docs/verification-matrix.md` and the frontend `DocsAndMatrixView` only with items backed by test/environment evidence.
2. **GUI feature-mapping review**: page by page and button by button against the §6 checklist; confirm API wiring, persistence, and failure states; mark static display and JSON-editing areas as incomplete.
3. **Backend completion**: wire in the Redis channel and validate failure recovery, surface the global audit chain in the frontend, a cleanup strategy for legacy vector collections (the reindex command is in place), model routing/fallback, connector OAuth, and the MCP stdio transport.
4. **Real Agent-chain acceptance**: on a selected CAPI / Metal3 / Ironic / OS version matrix, add end-to-end evidence phase by phase per the §5.1 seven-step state machine; gradually replace fail-closed prechecks with real execution adapters.
5. **Data plane and Day-2**: Multus/SR-IOV/OVS/Calico/DPDK install/rollback, real VF and traffic validation; scale-out/scale-in/upgrade/repair operations loop.

---

*This inventory is a review of the current code state and does not constitute a production-readiness claim; any external statement that something "is supported" should carry the specific environment, operation, and observable post-conditions as evidence.*
