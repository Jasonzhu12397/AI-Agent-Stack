# Rust Platform API

## Architecture decision

Rust/Axum in `apps/rust-api` is the sole supported SaaS Platform API runtime.
It is the application API itself, not an edge proxy, gateway in front of
another application, or compatibility layer.

Django, Django REST Framework, Django Channels, Daphne, Django Admin,
`manage.py`, and the Rust-to-Django proxy are retired. They must be removed
from application code, dependencies, images, Compose/Kubernetes manifests,
CI, scripts, health checks and operational documentation. There is no Django
upstream, proxy mode, fallback mode, dual-run mode or Django rollback target.

Python remains permitted for independent Workers, infrastructure Agents,
build tooling and offline utilities. Those processes do not own SaaS HTTP
routes, authentication, tenant/project authorization or platform business
transactions.

## Rust service responsibilities

- Implementation: Axum/Tokio in `apps/rust-api`.
- API routing: all browser and external API requests terminate directly in
  Rust-native handlers.
- Persistence: PostgreSQL is authoritative for durable platform state.
- Realtime: Rust-native WebSocket and streaming handlers; no Channels relay.
- Authentication: Rust-native session lifecycle and supported token
  authentication, with CSRF enforcement for cookie-authenticated mutations.
- Authorization: derive tenant and project scope from the authenticated
  principal and persisted membership; never trust client-supplied scope.
- Auditing: write audit records atomically with protected business mutations
  where the contract requires it.
- Resource controls: bounded request bodies, timeouts, WebSocket limits and
  tenant token-budget reservation/settlement.

## Configuration

Use only the configuration required by the Rust application and documented
in `apps/rust-api/src/config.rs`, including:

| Variable | Meaning |
| --- | --- |
| `RUST_API_LISTEN` | Rust API listen address |
| `DATABASE_URL` | PostgreSQL connection |
| `RUST_LOG` | Rust tracing filter |
| `RUST_API_SECURE_COOKIES` | Secure session/CSRF cookies in HTTPS deployments |
| `RUST_API_EXTERNAL_SCHEME` | Externally visible scheme for trusted ingress metadata |

The following are forbidden legacy proxy/bridge settings and must be removed
from code and deployment configuration: `RUST_API_UPSTREAM`,
`RUST_API_UPSTREAM_TIMEOUT_SECONDS`, `RUST_API_REQUEST_TIMEOUT_SECONDS`
when used only for upstream proxying, `RUST_API_NATIVE_IDENTITY` feature
switches, and `DJANGO_SECRET_KEY`.

## Local development

Build and test the Rust service directly:

```bash
cargo fmt --manifest-path apps/rust-api/Cargo.toml -- --check
cargo clippy --manifest-path apps/rust-api/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path apps/rust-api/Cargo.toml
cargo run --manifest-path apps/rust-api/Cargo.toml
```

The service connects directly to PostgreSQL. Vue Studio uses the Rust API
origin or same-origin API path. Do not start a Django service or a proxy
container as part of local development.

## Required removal and acceptance criteria

1. Remove all Django project packages, app code, management commands,
   migrations, admin, Channels consumers and Daphne entrypoints after their
   business behavior and schema ownership have been implemented in Rust or
   replaced with an explicit non-Django migration mechanism.
2. Remove the Rust proxy client, upstream URL configuration, proxy fallback
   routes and WebSocket relay. Rust handlers must implement the actual API
   behavior.
3. Remove Django/Python API images and services from every Compose and
   Kubernetes deployment. The API container must build from
   `apps/rust-api/Dockerfile` and execute the Rust binary directly.
4. Remove Django dependencies and commands from root/package dependency
   manifests, Dockerfiles, CI workflows, Codespaces launchers, backup
   schedulers, scripts and runbooks.
5. Move database schema lifecycle to Rust-owned SQL migrations or another
   explicitly selected migration tool. Do not execute Django migrations or
   require Django model metadata at runtime.
6. Implement every required API and realtime route natively in Rust, with
   integration coverage for authentication, tenant/project RBAC, audit,
   idempotency, error handling and resource limits.
7. Verify a clean Rust-only deployment: start, readiness, login, all API
   surfaces, WebSocket/SSE, background execution integration, backup/restore
   and shutdown. CI must build and smoke-test this deployment.
8. Rollback only to a previous compatible Rust API release. Never restore
   Django as an application runtime.

## Data and migration safety

Removing Django runtime code does not mean deleting or recreating production
PostgreSQL data. Preserve existing business records and credentials. Define
and test a forward-only, versioned schema migration path owned independently
of Django before removing the Django migration history. Take and verify
backups before destructive schema changes. Keep compatibility for existing
data until the Rust implementation has read/write parity and reconciliation
tests.

The repository's remaining Django references are removal work, not an
approved transitional architecture. Do not mark the migration complete until
the code, dependency graph, deployment assets and CI all satisfy the criteria
above.
