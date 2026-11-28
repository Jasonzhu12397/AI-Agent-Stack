# ADR-0001: Technology and deployment baseline

- Status: Accepted. Rust-only runtime.
- Date: 2026-11-02

## Context

AI Agent Studio requires a multi-tenant control plane, asynchronous execution, policy-governed tools, workflows, integrations, knowledge retrieval and infrastructure adapters. The frontend and backend should use independent development approaches.

## Decision

- Backend: Rust 1.85, Axum and SQLx; single binary serving REST and WebSocket traffic.
- Frontend: Vue 3, TypeScript, Vite, Element Plus, Pinia and Vue Router.
- Authoritative state: PostgreSQL. Redis is auxiliary cache/rate-limit/coordination infrastructure, never the sole durable task store.
- Local runtime: Docker Compose.
- Execution shape: modular Rust API, in-process asynchronous Worker with PostgreSQL task leases, and a future isolated plugin executor.
- Model/tool/MCP development: deterministic local fakes; a fake fixture is not protocol compatibility.
- Production: Kubernetes, OIDC and external secret manager are later-stage requirements.

## Consequences

Rust provides the HTTP/WebSocket surface, SQL migrations, native sessions, the audit chain and the asynchronous Worker. Vue is independently built and communicates through versioned REST APIs and WebSockets. Task execution is leased from PostgreSQL so long-running work is not tied to an HTTP request lifetime.

The Rust-only runtime must stay validated end-to-end. CI rejects any reintroduction of the retired Python web runtime or libvirt dependencies.

## Security

- No production credentials or production endpoints in local development.
- Tool execution is deny-by-default; model output is untrusted intent, not authorization.
- Credentials are represented by references; no secret values in prompts, traces, logs, snapshots or audit summaries.
- Real external write operations require a separately approved task card and specifically authorized test target.
