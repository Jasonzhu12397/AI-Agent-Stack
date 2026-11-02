# ADR-0001: Technology and deployment baseline

- Status: Accepted; supersedes the original FastAPI/React decision.
- Date: 2026-11-02

## Context

AI Agent Studio requires a multi-tenant control plane, asynchronous execution, policy-governed tools, workflows, integrations, knowledge retrieval and infrastructure adapters. The frontend and backend should use independent development approaches.

## Decision

- Backend: Python 3.12, Django, Django REST Framework and Django Channels/ASGI.
- Frontend: Vue 3, TypeScript, Vite, Element Plus, Pinia and Vue Router.
- Authoritative state: PostgreSQL. Redis is auxiliary cache/rate-limit/coordination infrastructure, never the sole durable task store.
- Local runtime: Docker Compose.
- Execution shape: modular Django control plane, independent Python Workers and a future isolated plugin executor.
- Model/tool/MCP development: deterministic local fakes; a fake fixture is not protocol compatibility.
- Production: Kubernetes, OIDC and external secret manager are later-stage requirements.

## Consequences

Django provides the application framework, ORM, migrations, authentication primitives and admin surface. Vue is independently built and communicates through versioned REST APIs and WebSockets. Workers remain separate processes so long-running execution is not coupled to HTTP request lifetimes.

The migration must be revalidated end-to-end. Previous FastAPI/React CI evidence is historical only.

## Security

- No production credentials or production endpoints in local development.
- Tool execution is deny-by-default; model output is untrusted intent, not authorization.
- Credentials are represented by references; no secret values in prompts, traces, logs, snapshots or audit summaries.
- Real external write operations require a separately approved task card and specifically authorized test target.
