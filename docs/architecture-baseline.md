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
