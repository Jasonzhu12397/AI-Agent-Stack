# Rust API edge

`apps/rust-api` is the Axum edge service for the platform API. It is the first
stage of moving the public API layer to Rust without breaking the existing
domain model, permissions, audit and realtime code in the Django service.

## What it does today

- Serves `/health/ready` from Rust, checking the upstream platform API and
  returning `503` when the upstream is unavailable.
- Serves `/api/v1/edge/info` with the edge service, version and upstream.
- Streams every other request and response through to the Django platform API,
  preserving the HTTP method, path, query string, request body, content type,
  cookies (`Authorization`, `sessionid`, CSRF), response status and streaming
  bodies.
- Removes hop-by-hop headers, adds `X-Forwarded-For`, `X-Forwarded-Proto` and
  generates `X-Request-Id` when the caller did not send one.
- Applies bounded upstream/request timeouts and returns a structured
  `UPSTREAM_UNAVAILABLE` error when the platform API cannot be reached.

Authentication, tenant resolution, RBAC and audit stay in Django during this
stage. The edge deliberately does not parse credentials, so a rollback is a
single routing change.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `RUST_API_LISTEN` | `0.0.0.0:8080` | Listen address |
| `RUST_API_UPSTREAM` | `http://127.0.0.1:8000` | Django origin, without a path |
| `RUST_API_UPSTREAM_TIMEOUT_SECONDS` | `3` | Capacity/health probe timeout |
| `RUST_API_REQUEST_TIMEOUT_SECONDS` | `60` | Full proxied request budget |
| `RUST_LOG` | unset | `tracing` filter, for example `info` |

The service refuses an upstream URL that carries a path, a non-HTTP scheme, a
zero timeout, or a request timeout shorter than the upstream timeout.

## Run locally

```bash
cd apps/rust-api
cargo run
curl -s http://127.0.0.1:8080/health/ready
curl -s http://127.0.0.1:8080/api/v1/edge/info
```

The platform API must already be listening on `RUST_API_UPSTREAM`. For a
containerised canary:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml --profile rust-edge up -d api-edge
curl -s http://127.0.0.1:8080/health/ready
```

The canary publishes port `8080`; the existing Django service keeps port `8000`,
so both paths are available and either can be used for comparison.

## Cutover and rollback

1. Start the canary and compare `/health/ready`, `/api/v1/auth/csrf/`, a
   logged-in task list request and a websocket handshake against the direct
   Django origin.
2. Point the frontend/reverse proxy at `api-edge` only after the comparison
   passes. Keep `RUST_API_UPSTREAM` pointed at the Django service.
3. Roll back by pointing the frontend/reverse proxy at the Django origin again;
   the Rust edge holds no state and needs no data migration.

WebSocket and SSE traffic currently has to terminate at Django because the edge
is an HTTP/1.1 streaming proxy. When task realtime moves into Rust, that
upgrade path and its authentication must be implemented before the proxy is
made the only public entry point.

## Migration sequence

The edge is the stable seam for moving routes into Rust:

1. Edge proxy (this stage) with parity checks and rollback.
2. Native Rust auth/session and tenant middleware, sharing the existing
   PostgreSQL schema, while other routes still proxy to Django.
3. Move model gateway, task read APIs and edge-native health/metrics.
4. Retire the proxied Django route only after a side-by-side comparison and
   rollback window.
