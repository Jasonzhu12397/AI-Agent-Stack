# Vibe Coding Execution Plan

## Delivery rules

- Implement one vertical slice per milestone.
- Inspect existing code before changing it.
- Write or update tests before implementation where practical.
- Run tests, lint, and type checks; report actual command output.
- Never claim a check passed unless it was executed successfully.
- Treat PostgreSQL as the authoritative persistence layer. Redis is cache/coordination only.
- Keep model output as structured intent. All side effects must pass schema, identity, allowlist, scope, and policy checks.
- Do not connect production infrastructure or perform external write operations in the MVP.

## Milestones

| Milestone | Scope | Exit evidence |
|---|---|---|
| M0 | Modular API, independent Worker, Postgres, Redis, Docker Compose, CI, health endpoints | Clean startup, migrations, tests, lint, documented setup |
| M1 | Agent draft/publish/version, asynchronous runs, FakeModel, Tool Router, FakeTool, audit/trace | Version immutability, tenant isolation, worker recovery tests |
| M2 | Workflow DAG: Start/Input, LLM, Tool, Condition, End | Publish validation and undeclared-cycle rejection |
| M3 | Approval bound to normalized plan hash, target, parameters, versions, and policy | Authorized one-time approval, expiry, re-review after mutation |
| M4 | Integration Hub, MCP client/local test server, Skills registry and binding | Contract tests and credential redaction |
| M5 | Kubernetes read-only adapter and CAPI/Metal3/Ironic compatibility contracts | Version matrix and simulation tests; no production writes |

## Explicitly out of scope

RAG, plugin marketplace, broad vendor connector catalog, multi-agent orchestration, production CAPI/Metal3 writes, enterprise SSO, and HA are not part of the initial MVP.

## M0 implementation notes

The initial slice provides API liveness/readiness endpoints, a separately deployed Worker process, PostgreSQL and Redis services, a Docker Compose environment, and CI checks. The Worker currently performs a database heartbeat only; durable task claiming, leases, retries, and recovery belong to M1.
