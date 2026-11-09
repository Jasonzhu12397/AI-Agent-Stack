# Architecture baseline V3.0

## Technology decision

- Frontend: Vue 3 + TypeScript + Vite + Element Plus + Pinia + Vue Router.
- Backend: Python 3.12 + Django + Django REST Framework.
- Realtime: Django Channels on ASGI; WebSocket consumers will be introduced with durable run streaming.
- Persistence: PostgreSQL is authoritative. Redis is auxiliary and never the only store for durable state.
- Execution: independent Python Workers; task claiming, leases and recovery are a dedicated milestone.
- Object storage: S3-compatible API. Vector retrieval: pgvector behind an abstraction.
- Deployment: Docker Compose locally; Kubernetes with Helm/Kustomize in production.

## Runtime boundaries

Django is the control plane: identity, tenant/project authorization, configuration, version publication, policy, audit and API orchestration.

Python Workers are the execution plane: Agent runs, workflow steps, scheduled work and isolated integration execution. Long-running work must not be tied to an HTTP request lifecycle.

Vue communicates with Django through versioned REST APIs and WebSocket channels. The browser is never authoritative for tenant identity, permissions, approval state or execution policy.

## Initial implementation status

The migration slice introduces the Django ASGI application, DRF API root, health endpoints, Vue 3 application shell, PostgreSQL settings and a standalone Python Worker heartbeat. This is not yet a complete authentication, task queue, Agent runtime or plugin system.

## Security invariants

- Production refuses the development secret key and DEBUG mode.
- DRF defaults to authenticated access; intentionally public endpoints must explicitly opt in.
- Server derives Principal and tenant scope; client-supplied tenant identifiers are untrusted.
- Model output, retrieval results, Skill content, MCP responses and plugin data are untrusted.
- Credentials are stored as secret references, never copied into prompts, logs or traces.
- Destructive or externally visible actions require policy evaluation and configured approval.
- Retries of non-idempotent side effects are not automatic.

## Deployment boundaries

API and Worker are separate processes built from the same Python package image. Web Studio is a separate Node/Vite application. PostgreSQL owns durable business state; Redis supports cache and coordination only. Production secrets must come from an external secret manager.


## Dual-agent infrastructure control extension

The general Studio runtime remains independent of infrastructure-specific execution. For bare-metal Kubernetes delivery it coordinates a Remote Control Plane Agent and two enrolled node-agent roles:

- EPH Node Agent: runs on the temporary management/EPH node, performs preflight and plan validation, discovers management-cluster CAPI/Metal3 API groups and later coordinates provisioning through a version-verified adapter.
- Target Cluster Agent: is installed only after the target Kubernetes API is reachable; it reports health and performs scoped read-only inspection before any separately authorized day-2 mutations are considered.
- CAPI, Metal3 and Ironic remain the infrastructure control chain. The platform submits declarative resources through adapters and verifies controller Conditions/Events and target-side postconditions.

Both agents initiate outbound HTTPS communication. Enrollment tokens are one-time and stored as hashes; command dispatch is PostgreSQL-backed, idempotent, lease-protected and bound to a node identity. Provisioning/bootstrap commands require a second administrator to approve the canonical plan hash. No arbitrary shell, Python, raw manifest or kubeconfig is accepted by the remote command API.

The first implementation supports node enrollment, heartbeat, command polling/results, revocation, hash-bound approval, EPH host preflight, validated cluster-plan dry runs and read-only in-cluster Kubernetes API inspection. It does not yet create CAPI/Metal3 resources or perform Ironic/BMC operations. Those writes remain disabled until the target Kubernetes/CAPI/Metal3/Ironic/IPA version matrix, schemas, templates, credentials and postcondition checks are verified. See \`docs/dual-agent-infrastructure-architecture.md\`.
