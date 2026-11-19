# AI-Agent-Stack Architecture Inventory (based on the real code on `main`)

> Language: English · [中文](./architecture-inventory.zh-CN.md)

> Scope: this inventory is checked only against the current code of repository `Jasonzhu12397/AI-Agent-Stack` on the `main` branch. Design goals, documentation plans, and frontend templates are not counted as completed features.
> Inventory date: 2026-11-19; reviewed baseline HEAD: `c0bd46de764104a129c7397d8d5666130a6f4c14`.

## 0. State definitions (shared vocabulary for this inventory)

| Label | Meaning | Criteria |
|---|---|---|
| 【Baseline】 Code baseline | An importable, test-covered implementation exists in the repo: a concrete directory, data model, view, execution function, or contract | Does not mean it has been proven to run in a real environment |
| 【Pending validation】 Implemented, pending validation | The code path is complete with unit tests or mock coverage, but lacks end-to-end acceptance in a real environment (real K8s / bare metal / real model provider / real vector store) | Failure and exception paths still need review against the real chain |
| 【To develop】 To be developed | Only a design, enum value, frontend template, or documentation plan exists; the runtime executor, adapter, or full UI interaction is not yet landed | Must not be advertised as supported |

## 1. Baseline evidence (this review)

- Local baseline (re-verified 2026-11-19): **935 passed**; `ruff check .` All checks passed; `manage.py check` 0 issues; `makemigrations --check --dry-run` reports no model drift.
- Test environment: no local PostgreSQL/Redis; tests run with an inline `DATABASE_URL=sqlite://` override; CI uses a PostgreSQL 16 service container.
- Scale: `apps/` + `packages/` Python source and tests total about 30.5k lines; 74 `tests/test_*.py` files.
- CI workflow: three jobs in `.github/workflows/ci.yml` — `python` (ruff / system check / migration drift / migrate / pytest), `web` (npm test / Vite build), `compose` (config validation of both compose files).
- History: the Python CI previously failed on three ruff import-ordering issues in `tests/test_worker_tasks.py` (not a test-logic failure); fixed in commit `5ba685b`. The re-run result of the remote CI was not re-checked this time.

## 2. Overall architecture overview (target form × real directory mapping)

Target form: **a general-purpose AI Agent platform + three infrastructure deployment Agents + the Metal3/Ironic/CAPI/Omni execution system**. AI Agents understand, plan, orchestrate, and call; the underlying infrastructure components perform the actual deployment. The platform itself is Multi-Agent; external AI Agents are plugged in through plugins/connectors and invoked by orchestration.

| Layer | Target components | Real code location | Current state |
|---|---|---|---|
| User entry | Web UI / Chat / Workflow / Agent management / plugin marketplace / operations console | `apps/web-studio/src/components/`, `src/services/api.ts` | 【Baseline】 Pages and API wiring exist; per-interaction acceptance still to be checked (see §6) |
| Platform backend | API Gateway / Auth / Agent Runtime / Workflow Engine / Model Router / Plugin·MCP Gateway / Knowledge·RAG / Token Budget / Audit·RBAC / Task Queue / Realtime Logs / Events | `apps/platform-api/core/`, `apps/worker/worker_app/` | 【Baseline】 Most modules implemented; realtime push, model routing/fallback, and the MCP runtime remain to be developed (see §3) |
| Infrastructure Agent control plane | Cloud Control / EPH / Target Agents | `apps/cloud_control_agent/`, `apps/eph_agent/`, `apps/target_agent/` | 【Baseline】 Independent entrypoints, protocols, and state machines exist for all three; the real bare-metal chain awaits validation (see §4) |
| Agent contracts & transport | Identity / command / lease / heartbeat / checkpoint / mTLS | `packages/agent_contracts/`, `packages/agent_transport/`, `apps/agent_contracts/` | 【Baseline】 Contracts and transport implemented (see §7) |
| Execution layer | CAPI / Metal3 / Ironic·BMO / Omni / BMH Inventory / Kubeadm·K8s / Chrony·NTP / SR-IOV·DPDK·Multus | Skill/adapter modules inside the three Agents call `kubectl` / `clusterctl` / the Ironic HTTP client | 【Pending validation】 Execution adapters are mostly read-only observation plus fail-closed validation; real install/migration/data-plane forwarding awaits acceptance (see §5) |
| Physical bare metal | Servers / BMC·IPMI·Redfish / management network / control-plane network / data-plane network | `apps/platform-api/core/bmc_credentials.py`, fleet inventory views; BMC config persistence | 【Baseline】 Credential and inventory models exist; real BMC/Ironic power-on acceptance to be developed |

## 3. Platform layer: general-purpose AI Agent capabilities, item by item

| Module | Target responsibility | Code evidence | State | Gaps / notes |
|---|---|---|---|---|
| API Backend | Public API, users, projects, task management | DRF routes cover auth / tenant / project / task / model-provider / model-config / agent / agent-run / workflow / run / knowledge / connector / installation / connection / cluster-deployment / fleet / agent-node / command / approval / audit / member / token-budget / sriov / handoff / postflight (`core/urls.py`) | 【Baseline】 | Error states, pagination, and permission boundaries still to be checked per endpoint |
| Agent Runtime | Agent lifecycle, context, tool calls | `apps/worker/worker_app/tasks.py`: DB task records, atomic claim, lease token/expiry, heartbeat renewal, bounded retries, cooperative cancel; `core/agent_views.py`: Agent definition / version / publish / run records | 【Baseline】 | Context retention and failure recovery across multi-step tool calls still to be reviewed against the real model chain |
| Workflow Engine | Multi-step orchestration, state transitions | `core/workflow_engine.py`: node types `start / llm / condition / knowledge_search / tool / end`; `core/workflow_views.py`: validate / publish / runs | 【Baseline】 | 【To develop】 Parallel branches, loops, human-approval nodes and approval resume; there are currently no `parallel / loop / approval` node types |
| Model Router | Unified access and routing for domestic and foreign models | `core/model_gateway.py`: `ProviderType` is only `fake` and `openai_compatible`; chat `/chat/completions`, embeddings `/embeddings` over HTTP; errors marked retryable on 5xx/429 | 【Baseline】 | 【To develop】 Multi-provider primary/standby routing, fallback chain, degradation; domestic providers are reached via OpenAI-compatible endpoints, no native SDK; FAKE is test-only |
| Plugin / MCP Gateway | Plugins, MCP Server, third-party system integration | `core/plugin_views.py`: connector catalog, approval, project install, explicit grant, connection records; `core/http_connector.py`: registers only `http` / `openapi` tools; `core/models.py` connector enum includes `mcp` | 【Baseline】 (HTTP/OpenAPI part) | 【To develop】 Full MCP protocol runtime: no jsonrpc / stdio / SSE / `tools/list` implementation found in code; OAuth authorization-code flow and token refresh are not implemented (credentials currently resolve only bearer / env references); real provider integration awaits acceptance |
| Knowledge / RAG | Documents, vector retrieval, knowledge bases | `core/knowledge_models.py`, `core/knowledge_views.py`, `core/rag_runtime.py`, `core/vector_store.py`: KB/document/embedding-model binding; `QdrantVectorStore` (urllib HTTP, needs `QDRANT_URL` / collection / api-key env references); local cosine similarity | 【Baseline】 | 【Pending validation】 Full ingest/retrieve chain against a real Qdrant instance + embedding model; 【To develop】 pgvector and S3-compatible object storage are not yet wired in code (see §7.3) |
| Token Budget | Budget reservation, usage settlement, overspend protection | `core/token_budget.py`, `core/token_budget_views.py`: tenant-level reservation, settlement, idempotency, overspend protection; includes regression tests | 【Pending validation】 | Local tests are green after several rounds of fixes; settlement consistency still to be reviewed under concurrent workers and real provider usage reporting |
| Task Queue / Scheduler | Cache, task queue, async execution | `apps/worker/worker_app/main.py` polls `process_once`; task claim and lease are based on PostgreSQL records; `redis` is in dependencies, `settings.py` only defines `REDIS_URL` | 【Baseline】 (DB queue) | 【To develop】 Redis actually used as cache / queue / coordination channel: no `CACHES`, channel_layer, or redis client call found in code; queue failure recovery, backlog, and replay still to be validated |
| Audit / RBAC | Permissions, tenant isolation, operation records | `core/membership_views.py`: tenant/project members and roles (admin/editor/viewer); views scoped by `memberships__user`, non-admins filtered again by project membership; `core/audit.py` chained-hash audit records and verify; persistence covers the agent-node / agent-plugin / cluster-deployment / membership / plugin / tool-approval / model / knowledge / agent / workflow / task / token-budget / tenant / project write endpoints | 【Baseline】 | Login/logout and other authentication events have no tenant context, so they are not chained yet (the hash chain is built per tenant and needs a separate design); policy loop to be fully covered |
| Realtime Logs / Events | Realtime logs for Agents, Workflow, deployments | Frontend `api.ts` pulls primarily via requests; no channels consumer / `group_send` / channel_layer implementation found in code | 【To develop】 | Realtime push channel (WebSocket/SSE) not landed yet; current experience is polling |
| Multi-Agent onboarding | External AI Agents plugged in as plugins to collaborate | Current onboarding shape: third-party capabilities packaged as connector tools (HTTP/OpenAPI schema), invoked by Agent/Workflow after tool-router permission/approval | 【Baseline】 (tool-level onboarding) | 【To develop】 Full onboarding protocol that treats an external AI Agent as an orchestratable node returning structured results and participating in state transitions; MCP Agent onboarding awaits the runtime |

### 3.1 Planned plugin and model integrations (current real state)

| Category | Planned items | Code state | State |
|---|---|---|---|
| Maps | Amap, Baidu Maps, Google Maps | Frontend `src/domain/connectorTemplates.ts` holds HTTP template metadata for amap places/geocoding/routing, baidu places/geocoding, google places | 【Baseline】 (template/HTTP contract declaration); real connection, auth, and backend-call acceptance pending validation |
| Weather | Weather API / weather service | Template includes open-meteo (HTTP, no key) | 【Baseline】 (template); real connector pending acceptance |
| Dev collaboration | GitHub, Jira | Template includes `github-readonly` (HTTP); no Jira template or dedicated executor | GitHub read-only 【Baseline/Pending validation】; Jira 【To develop】 |
| Office collaboration | Microsoft 365, Teams | Template includes `microsoft-graph-readonly` (HTTP); no dedicated Teams connector | 【Baseline】 (read-only template); full Graph scopes, OAuth, and Teams integration 【To develop】 |
| Extensibility | MCP, third-party APIs, custom plugins | Connector catalog/install/grant/connection-test framework exists; executor is HTTP/OpenAPI only; MCP is an enum placeholder | Framework 【Baseline】; MCP runtime and isolated custom-plugin execution 【To develop】 |
| Models | Domestic and foreign providers, OpenAI-compatible, routing and fallback | Gateway is only `openai_compatible` + `fake`; the OpenAI-compatible channel can be reused to reach any compatible endpoint | 【Baseline】 (compatible channel); provider coverage, explicit routing, primary/standby fallback 【To develop】 |

> The connector templates above are product planning and metadata declarations; they do not mean the corresponding OAuth flow, real provider integration, or production-grade credential custody are complete.

## 4. Infrastructure deployment layer: actual division of the three Agents

### 4.1 Code facts (authoritative, used to align documentation)

| Agent | Directory | Implemented actions / modules | State | Gaps |
|---|---|---|---|---|
| Cloud Control Agent | `apps/cloud_control_agent/` (`api.py`, `main.py`) | HTTP control plane: action/role validity check, reported capabilities check, checkpoint actor transition check, certificate CN identity check, deployment status aggregation and DB persistence | 【Baseline】 | 【To develop】 Real cloud-provider (Alibaba Cloud / Tencent Cloud / AWS) VM orchestration adapter: no cloud SDK found in `pyproject.toml` or code; this is currently a control-plane protocol and validation layer, not a cloud resource orchestrator |
| EPH Agent | `apps/eph_agent/` (`main.py`, `bootstrap_renderer.py`, `control_stack.py`, `control_stack_adapter.py`, `metal3_skill.py`, `capi_renderer.py`, `ntp_policy.py`, `templates/`) | Actions: `eph.preflight`, `eph.source.observe`, `eph.bootstrap.render` (pure kubeadm init / NoCloud seed / Agent Deployment rendering with artifact digests and no cluster writes), `eph.controlstack.apply` (idempotent cert-manager→CAPI→BMO→Ironic→CAPM3 apply plus readiness probes), `eph.metal3.bmh.apply`/`observe`/`wait_ready`, `eph.capi.cluster.apply`/`observe`/`wait_ready`; reported capabilities are generated from the single source in `packages/agent_contracts/orchestration.py` | 【Baseline】 | 【Pending validation】 Single-node Bootstrap cluster and target Control Plane bootstrapping to be accepted on real CAPI/Metal3/Ironic versions; there is **no** pivot adapter under the EPH directory (see conflict list §8) |
| Target Agent | `apps/target_agent/` (`main.py`, `capi_pivot.py`, `handoff.py`, `metal3_ownership.py`, `network_plugins.py`, `network_stack_adapter.py`, `cleanup_scope.py`, `eph_cleanup.py`, `worker_provisioning.py`, `postflight.py`, `ntp_attestation.py`, `ntp_policy.py`, `ironic_client.py`, `leader_election.py`, `worker_attestation.py`) | Actions: `target.cluster.observe`, `target.capi.observe`, `target.capi.pivot` (with dry-run), `target.metal3.observe`/`verify_ownership`, `target.worker.ntp.validate`, `target.ntp.control_plane.render`/`verify_bootstrap`, `target.ntp.internal.probe`, `target.network.apply` (idempotent Calico/Multus/OVS/SR-IOV/DPDK apply plus readiness observation), `target.handoff.evaluate`, `target.eph.cleanup`, `target.worker.provision`; pivot adapter, autonomous handoff state machine, BMH identity-set and ownership validation, EPH cleanup scope, worker provisioning, NTP attestation, three-replica Leader Election, Ironic HTTP client | 【Baseline】 | 【Pending validation】 Autonomous handoff once the real cluster is ready, CAPI Pivot, BMH migration, EPH cleanup, physical worker deployment and hardware acceptance; currently mostly fail-closed prechecks plus unit/mock coverage; the real provider matrix has not been run through |
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

Implementation: `core/cluster_deployment_services.py` (ordered phases: preflight → pivot → verify → worker provision → postflight → plan), together with phase transitions and checkpoint CAS in `core/agent_node_views.py` (sequence/phase compare-and-swap).

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
| Data Plane (SR-IOV/DPDK) | Pod data traffic, VFs, multi-NIC | SR-IOV Device Plugin config validation contract (`apps/agent_contracts/sriov_device_plugin.py`), view persistence, postflight resource-name check; Calico/Multus/DPDK appear as compatibility-determination fields (read-only validation) | SR-IOV config 【Baseline/Pending validation】; real install/upgrade/rollback of OVS/Calico/Multus/DPDK and real data-plane traffic validation 【To develop】 |

### 5.3 NTP rules

- Control Plane: uses external NTP.
- Worker: chronyd must use the Ironic internal IP/VIP of the target K8s Control Plane as the time sync source; external IP pools and fallback are forbidden.
- Code state: `eph_agent/ntp_policy.py`, `target_agent/ntp_policy.py`, `target_agent/ntp_attestation.py` implement policy validation and internal-source probing; generate chrony host commands / templates.
- State: policy validation and command generation 【Baseline】; the generated host commands are not yet injected into CAPI bootstrap templates or executed on physical hosts, so real NTP sync acceptance is 【Pending validation/To develop】.

## 6. Frontend GUI inventory (`apps/web-studio/src/components/`)

Acceptance bar: every button connects to a real API, can persist data, execute the action, and handle failure states correctly; static display alone does not count as complete.

| Page | Component | Code state | State | Gaps |
|---|---|---|---|---|
| Login | `LoginView.vue` | Calls `/api/v1/auth/csrf