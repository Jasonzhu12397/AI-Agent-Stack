#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="$ROOT_DIR/.env.cloud"
COMPOSE_FILE="$ROOT_DIR/deploy/docker-compose.cloud.yml"
WEB_PORT="${WEB_PORT:-$(sed -n 's/^WEB_PORT=//p' "$ENV_FILE" | tail -n 1)}"
WEB_PORT="${WEB_PORT:-8080}"

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

[ -f "$ENV_FILE" ] || fail "CLOUD_ENV_FILE_NOT_FOUND"
chmod 600 "$ENV_FILE"
command -v docker >/dev/null 2>&1 || fail "DOCKER_NOT_INSTALLED"
docker compose version >/dev/null 2>&1 || fail "DOCKER_COMPOSE_V2_REQUIRED"
command -v python3 >/dev/null 2>&1 || fail "PYTHON3_NOT_INSTALLED"
command -v curl >/dev/null 2>&1 || fail "CURL_NOT_INSTALLED"

python3 - "$ENV_FILE" <<'PY'
import os
import sys

values = {}
with open(sys.argv[1], encoding="utf-8") as source:
    for raw_line in source:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")

required = (
    "SECRET_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "QDRANT_URL",
    "ALLOWED_HOSTS",
    "CSRF_TRUSTED_ORIGINS",
    "CORS_ALLOWED_ORIGINS",
)
missing = [key for key in required if not values.get(key)]
if missing:
    raise SystemExit("CLOUD_ENV_REQUIRED_VALUES_MISSING:" + ",".join(missing))
if values.get("APP_ENV", "production") != "production":
    raise SystemExit("APP_ENV_MUST_BE_PRODUCTION")
if values.get("DEBUG", "false").lower() != "false":
    raise SystemExit("DEBUG_MUST_BE_FALSE")
secret = values["SECRET_KEY"]
if secret.startswith("replace-") or secret == "local-only-change-before-production" or len(secret) < 32:
    raise SystemExit("PRODUCTION_SECRET_KEY_INVALID")
for key in ("DATABASE_URL", "REDIS_URL", "QDRANT_URL"):
    if "example.net" in values[key] or "replace-me" in values[key]:
        raise SystemExit("CLOUD_ENDPOINT_PLACEHOLDER:" + key)
if "example.com" in values["ALLOWED_HOSTS"]:
    raise SystemExit("ALLOWED_HOSTS_PLACEHOLDER")
for key in ("CSRF_TRUSTED_ORIGINS", "CORS_ALLOWED_ORIGINS"):
    origins = [origin.strip() for origin in values[key].split(",")]
    if not all(origin.startswith("https://") for origin in origins):
        raise SystemExit("HTTPS_ORIGINS_REQUIRED:" + key)
    if any("example.com" in origin for origin in origins):
        raise SystemExit("HTTPS_ORIGIN_PLACEHOLDER:" + key)
profiles = [item.strip() for item in values.get("COMPOSE_PROFILES", "").split(",")]
mtls_required = values.get("AGENT_MTLS_REQUIRED", "false").lower() == "true"
if "agent-mtls" in profiles and not mtls_required:
    raise SystemExit("AGENT_MTLS_REQUIRED_FOR_INGRESS_PROFILE")
if mtls_required:
    if "agent-mtls" not in profiles:
        raise SystemExit("AGENT_MTLS_PROFILE_REQUIRED")
    subnet = values.get("AGENT_MTLS_NETWORK_SUBNET", "172.30.250.0/24")
    trusted = [
        item.strip()
        for item in values.get("AGENT_MTLS_TRUSTED_PROXY_CIDRS", "").split(",")
        if item.strip()
    ]
    if subnet not in trusted:
        raise SystemExit("AGENT_MTLS_TRUSTED_PROXY_CIDR_MUST_INCLUDE_NETWORK_SUBNET")
    for key in ("AGENT_MTLS_SERVER_PEM", "AGENT_MTLS_CLIENT_CA_PEM"):
        path = values.get(key, "")
        if not path or not os.path.isfile(path):
            raise SystemExit("AGENT_MTLS_CERTIFICATE_FILE_REQUIRED:" + key)
PY

cd "$ROOT_DIR"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config --quiet
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up --build -d

if ! curl --retry 30 --retry-all-errors --retry-delay 2 --max-time 8 --fail "http://127.0.0.1:$WEB_PORT/health/ready"; then
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" logs --tail=200 api worker web
    fail "CLOUD_CONTROL_PLANE_HEALTHCHECK_FAILED"
fi
curl --fail --silent "http://127.0.0.1:$WEB_PORT/" >/dev/null
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps
printf '%s\n' "CLOUD_CONTROL_PLANE_DEPLOYED"
