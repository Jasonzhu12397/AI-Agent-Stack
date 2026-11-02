# ADR-0001: Technology and deployment baseline

- Status: Accepted for the initial implementation; revisit when measured requirements justify a change.
- Date: 2026-11-02

## Context

The repository is an early scaffold. The product requirements call for a multi-tenant AI Agent Studio with asynchronous execution, policy-governed tools, workflows, integrations, knowledge retrieval, and infrastructure adapters. The initial architecture should be operable locally without granting access to production systems.

## Decision

- API: Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic.
- Web: React 18, TypeScript, Vite.
- Authoritative state: PostgreSQL. Redis is optional cache/rate-limit/coordination infrastructure and is never the sole durable task store.
- Local runtime: Docker Compose.
- Execution shape: modular control-plane API, independent workers, and a future isolated plugin executor. Keep module boundaries explicit before considering service decomposition.
- Model/tool/MCP development: deterministic local fakes. A fake MCP fixture is not evidence of protocol compatibility.
- Production: Kubernetes deployment, OIDC and external secret manager remain later-stage decisions and are not enabled by this ADR.

## Security consequences

- No production credentials or production endpoints in local development.
- Tool execution is deny-by-default; model output is untrusted intent, not authorization.
- Credentials are represented by references; no secret values in prompts, traces, logs, snapshots or audit summaries.
- Any real external write operation requires a separately approved task card and a specifically authorized test target.

## Verification

- GitHub Actions run #8 for commit 1fabafba3586af7f0ff085519c858fac0aa5957c passed dependency installation, Ruff and the currently configured pytest suite.
- Docker Compose startup, database migration round-trip, frontend build and end-to-end API/UI checks have not yet been verified.
- Exact external provider SDKs, OAuth scopes, MCP SDK/transport, and Kubernetes/CAPI/Metal3/Ironic API versions are intentionally undecided pending verification.
