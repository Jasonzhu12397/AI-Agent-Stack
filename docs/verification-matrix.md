# Verification matrix

Evidence is tied to immutable commits and CI runs. A file existing is not proof of behavior.

## Historical verification: FastAPI + React

[GitHub Actions run 41404656581](https://github.com/Jasonzhu12397/AI-Agent-Stack/actions/runs/41404656581), commit `71d3ae6684902befc8ca851ebe98705571af83c3`, passed the former FastAPI/React stack. This does not verify the Django/Vue migration.

## Latest verified commit

[GitHub Actions run 41424480053](https://github.com/Jasonzhu12397/AI-Agent-Stack/actions/runs/41424480053), commit `dea84b79ccef43a2c962f0407988e2f5a4521174`, completed successfully across Python, Vue and Compose jobs. Pytest: 89 passed. Ruff, Django checks, migration drift check, PostgreSQL migrations, Vue production build and Compose API/Web/Worker smoke tests passed. This run includes node enrollment, command approval/lease handling, read-only Kubernetes access, dynamic CAPI/Metal3 API discovery, the Vue dashboard and dual-agent console. Later cluster-plan completeness, renewable command lease, approval expiry and pod rollout changes are being revalidated.

[Durable worker run 41409745762](https://github.com/Jasonzhu12397/AI-Agent-Stack/actions/runs/41409745762), commit `287fe2daab5470a7326f2b2f1320e60f9cc62a23`, passed with 36 tests.

[Session authentication run 41408491282](https://github.com/Jasonzhu12397/AI-Agent-Stack/actions/runs/41408491282), commit `7bc5cb4f33b0911f2648b5302e557c694cd0863e`, passed with 28 tests. [Membership administration run 41408139658](https://github.com/Jasonzhu12397/AI-Agent-Stack/actions/runs/41408139658) passed with 26 tests.

[Membership administration run 41408139658](https://github.com/Jasonzhu12397/AI-Agent-Stack/actions/runs/41408139658), commit `a5629956683bd5a246149b786e67c8a4b1bd1862`, passed with 26 tests before the authentication endpoints were added.

## Django + Vue migration

| Area | Target | Status |
|---|---|---|
| Backend | Django 5.1+ / Python 3.12 | Passed in run 41405916271 |
| REST API | Django REST Framework | Tenant/project APIs, session auth, durable task APIs, Agent/Workflow APIs and new node enrollment/command/approval/runtime routes; new routes are awaiting PR CI |
| ASGI | Django Channels / Daphne | HTTP ASGI entrypoint passed in Compose smoke test; WebSocket consumers not implemented |
| Production configuration | Reject default SECRET_KEY and DEBUG=true | Negative test passed in run 41406654505 |
| Database | PostgreSQL via dj-database-url | Migration drift check and PostgreSQL migrations passed in run 41407102851 |
| Health | Liveness and database readiness | Both endpoints passed in Compose smoke test, run 41407102851 |
| Worker | Separate Python process / durable PostgreSQL queue | Atomic `select_for_update(skip_locked)` claim, attempt counter, lease owner/token, lease expiry recovery, heartbeat renewal, bounded retries, cooperative cancellation and persisted terminal states. Demo, single-turn LLM Agent and basic Workflow Run handlers are connected; Workflow execution evidence is in run 41420838331 |
| Frontend | Vue 3 + TypeScript + Vite | Vue navigation for Overview, Model Manager, Agent Studio, Workflow DAG and Infrastructure Agents; dual-agent console and dashboard reference port added. Core navigation build passed in run 41423617275; DashboardView changes are pending newer CI |
| UI components | Element Plus | Installed and registered |
| State management | Pinia | Installed and registered |
| Routing | Vue Router | Dependency selected; route table not yet implemented |
| Compose | API, Worker, PostgreSQL, Redis and Vue | Build/start and API/Web/Worker smoke tests passed in run 41407102851 |

## Not yet implemented or verified

| Area | Target | Current status |
|---|---|---|
| Identity and authorization | Server-derived Principal, tenant/project RBAC | Partial: Django session login/logout/current-user, CSRF protection, tenant/project RBAC, membership administration, last-owner/last-project-admin protections and membership revocation tested. OIDC/OAuth2, invitations/email workflow, password reset, service identities and fine-grained resource/tool policies remain unimplemented. |
| Durable task queue | Atomic claim, lease, heartbeat, expiry recovery, idempotency | Implemented for safe demo tasks and text-only Agent Runs; tests cover idempotent enqueue, role authorization, completion, stale lease rejection, lease recovery, retryable failure path and cancellation. No external side-effect handlers or Workflow Engine integration yet |
| Agent runtime | Run snapshots, model routing, Tool Router, durable steps | Partial: published Agent versions and asynchronous single-turn LLM runs work. Multi-step tool calling, model routing/fallback, memory and tool policy integration remain unimplemented |
| Workflow engine | Versioned DAG, branches, recovery | Partial: immutable versions; start/LLM/condition/end nodes; DAG validation; branch skip; persisted node steps; idempotent run creation. No drag/drop canvas, loop/parallel/join, human approval, HTTP/Code nodes, scheduler/webhook triggers or fault-injection proof of process restart recovery |
| Policy and approval | Default deny, hash-bound one-time approval | Partial: infrastructure provisioning/bootstrap commands require a second tenant administrator and canonical plan-hash match before dispatch. General Tool Router policy, expiry/revocation checks at every tool call and complete approval UI remain unimplemented |
| Real MCP | SDK, transport, negotiation and protocol tests | Not implemented; FakeMCP is an in-process fixture |
| Plugin isolation / SSRF | Sandboxed executor and network egress controls | Not implemented |
| RAG / Memory | ACL-filtered ingestion, retrieval and deletion | Not implemented |
| Vendor connectors | GitHub/Jira/Graph/Maps/Weather and others | Not implemented / no live API verification |
| Kubernetes/CAPI/Metal3/Ironic | Target CRD/API versions and test-cluster operations | Partial: node-agent enrollment, heartbeat, command leasing and read-only Kubernetes API inspection are implemented in this branch; CAPI/Metal3 API group discovery is available. Actual Cluster/MachineDeployment/BareMetalHost writes, Ironic/IPA/BMC operations, target-agent bootstrap and end-to-end cluster postconditions are disabled and not verified |
| Production readiness | OIDC, secret manager, backup/restore, HA, SLO and security scans | Not implemented / not verified |

## Evidence policy

- Link verified items to immutable commits and CI runs or reproducible local command output.
- Tests that are skipped or absent remain unverified.
- Simulations are labeled as simulations, not live integrations.
- A successful migration CI run is required before this stack is called validated.


## Dual-agent infrastructure slice (branch: feature/dual-agent-control-plane)

| Capability | Implementation | Evidence / limitation |
|---|---|---|
| EPH and target node enrollment | Tenant/project-scoped AgentNode records; one-time random token returned once; only SHA-256 hash stored; revoke cancels queued commands | Code and API tests added; awaiting PR CI |
| Remote command queue | PostgreSQL AgentCommand, idempotency key, node binding, attempts, lease token and 90-second lease; stale lease results rejected | Code and API tests added; awaiting PR CI |
| Approval gate | Cluster provision and target-agent bootstrap begin in WAITING_APPROVAL; separate tenant admin approves canonical payload hash | Self-approval and hash mismatch protection added; awaiting PR CI |
| Node agent runner | Python stdlib HTTPS client; outbound heartbeat/poll/result; compiled command allowlist; no shell execution | 83-test Python CI passed in run 41423617275, including host preflight, cluster plan dry-run, Kubernetes read-only operations and output redaction |
| EPH safe operations | Host preflight; cluster-plan validation for provider versions, network/image references, BMH role capacity, boot MAC, root-device hints, network-data and credential references; dynamic CAPI Cluster/Machine and Metal3 BareMetalHost read-only inspection | Logic/contract tests added; no live management cluster tested |
| Target cluster safe operations | In-cluster ServiceAccount GET requests for Kubernetes version, nodes, namespaces, pods/events/services; sanitized resource summaries, readiness and warning-event summaries | Unit tests added; no live target cluster tested |
| CAPI/Metal3/Ironic provisioning | Version-aware declarative adapter, resource writes, Ironic/BMC lifecycle and target-agent bootstrap | Not implemented; must wait for exact target version/CRD and credential verification |
| Production agent identity | mTLS or short-lived workload identity, certificate rotation and external secret delivery | Development one-time bearer token only; production deployment not verified |
| Vue infrastructure console | Reference-style dark navigation, project-scoped dashboard, EPH/Target enrollment, heartbeat, typed command dispatch, command history, approval hash and EPH cluster-plan wizard | Dashboard and navigation passed run 41423783202; cluster-plan wizard and pod rollout additions are awaiting newer CI |

| Kubernetes agent deployment | Separate EPH/Target Deployments, ServiceAccounts, read-only RBAC, non-root security context, readiness/liveness checks | Templates added under `deploy/agents/`; YAML/application not yet tested against a real management or target cluster |
