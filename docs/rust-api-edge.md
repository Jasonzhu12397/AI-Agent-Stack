# Rust Platform API

## Architecture decision

Rust/Axum in `apps/rust-api` is the sole supported SaaS Platform API runtime.

Django, Django REST Framework, Django Channels, Daphne, Django Admin and
`manage.py` are retired. They must not be used as an API runtime, reverse-proxy
upstream, fallback, migration dependency or operator workflow. Python remains
permitted for independent Workers, infrastructure Agents, build tooling and
offline utilities; those processes do not own HTTP API routes or platform
identity/authorization decisions.

The browser and external clients communicate with the Rust API only. Rust owns
authentication, session lifecycle, CSRF, tenant/project RBAC, business
transactions, audit, token budgets, model/provider configuration, plugins,
knowledge, workflows, task APIs and realtime connections.

## Rust service

- Implementation: Axum/Tokio in `apps/rust-api`.
- Persistence: PostgreSQL is authoritative for durable platform state.
- Realtime: Rust-native WebSocket and streaming handlers; no Channels relay.
- Authentication: Rust-native session and `X-Session-Token` support, with
  CSRF enforcement for cookie-authenticated mutations.
- Authorization: derive tenant and project scope from the authenticated
  principal and persisted membership; never trust client-supplied scope.
- Auditing: write audit records in the same transaction as the protected
  business mutation where the contract requires atomicity.
- Resource controls: bounded request bodies, timeouts, WebSocket limits and
  tenant token-budget reservations/settlement.

## Configuration

| Variable | Meaning |
| --- | --- |
| `RUST_API_LISTEN` | Rust API listen address |
| `DATABASE_URL` | PostgreSQL connection |
| `RUST_LOG` | Rust tracing filter |
| `RUST_API_SECURE_COOKIES` | Secure session/CSRF cookies in HTTPS deployments |
| `RUST_API_EXTERNAL_SCHEME` | Externally visible scheme used when constructing trusted forwarding metadata |

There is no `RUST_API_UPSTREAM`, `DJANGO_SECRET_KEY`,
`RUST_API_NATIVE_IDENTITY` or Django compatibility mode in the target
architecture. Transitional proxy code and configuration are migration debt and
must be removed rather than enabled in any deployment.

## Local development

Build and test the Rust service:

```bash
cargo fmt --manifest-path apps/rust-api/Cargo.toml -- --check
cargo clippy --manifest-path apps/rust-api/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path apps/rust-api/Cargo.toml
cargo run --manifest-path apps/rust-api/Cargo.toml
```

The service requires a reachable PostgreSQL database and the environment
variables documented by its configuration module. The web client must use the
same-origin Rust API base path (normally `/api/v1` behind the web ingress).

## Deployment acceptance

A deployment is not considered migrated merely because the Rust edge is
reachable. All of the following must be true:

1. The API container is built from `apps/rust-api/Dockerfile` and starts the
   Rust binary directly.
2. No Django/Daphne API container, proxy upstream, fallback process, admin
   endpoint or `manage.py` command is present in the Compose/Kubernetes
   manifests, entrypoints, health checks or operational runbooks.
3. Every frontend API and realtime request terminates in Rust-native handlers.
4. Every API route has a Rust handler and integration tests for authentication,
   tenant/project RBAC, audit behavior, error handling and idempotency where
   applicable.
5. Database schema ownership and migrations are independent of Django runtime
   packages; deployment does not invoke Django migration commands.
6. CI builds the Rust production image and performs API smoke tests against the
   Rust-only deployment profile.

## Migration debt requiring repository-wide verification

Older deployment assets may still contain transitional Django commands,
Python API images, Django schema/migration assumptions or Rust-to-Django proxy
settings. These are not supported architecture. Search and remove them from
the actual build, deployment and CI paths before declaring the production
cutover complete. Do not reintroduce Django as a rollback target; rollback must
use a previous Rust API image and a backward-compatible database migration.
