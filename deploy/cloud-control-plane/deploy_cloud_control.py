"""Deploy the cloud control plane compose stack, fail-closed on config.

Python replacement for the former ``deploy/cloud-control-plane/deploy.sh``:
the same ``.env.cloud`` checks run in-process (no shelled-out Python), then
compose builds, starts and passes a readiness probe before
``CLOUD_CONTROL_PLANE_DEPLOYED`` is printed.

Usage:
    python3 deploy/cloud-control-plane/deploy_cloud_control.py
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT_DIR / ".env.cloud"
COMPOSE_FILE = ROOT_DIR / "deploy" / "docker-compose.cloud.yml"

REQUIRED_VALUES = (
    "SECRET_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "QDRANT_URL",
    "ALLOWED_HOSTS",
    "CSRF_TRUSTED_ORIGINS",
    "CORS_ALLOWED_ORIGINS",
)


def fail(code: str) -> None:
    print(code, file=sys.stderr)
    raise SystemExit(1)


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def validate(values: dict[str, str]) -> None:
    missing = [key for key in REQUIRED_VALUES if not values.get(key)]
    if missing:
        fail("CLOUD_ENV_REQUIRED_VALUES_MISSING:" + ",".join(missing))
    if values.get("APP_ENV", "production") != "production":
        fail("APP_ENV_MUST_BE_PRODUCTION")
    if values.get("DEBUG", "false").lower() != "false":
        fail("DEBUG_MUST_BE_FALSE")
    secret = values["SECRET_KEY"]
    if (
        secret.startswith("replace-")
        or secret == "local-only-change-before-production"
        or len(secret) < 32
    ):
        fail("PRODUCTION_SECRET_KEY_INVALID")
    for key in ("DATABASE_URL", "REDIS_URL", "QDRANT_URL"):
        if "example.net" in values[key] or "replace-me" in values[key]:
            fail("CLOUD_ENDPOINT_PLACEHOLDER:" + key)
    if "example.com" in values["ALLOWED_HOSTS"]:
        fail("ALLOWED_HOSTS_PLACEHOLDER")
    for key in ("CSRF_TRUSTED_ORIGINS", "CORS_ALLOWED_ORIGINS"):
        origins = [origin.strip() for origin in values[key].split(",")]
        if not all(origin.startswith("https://") for origin in origins):
            fail("HTTPS_ORIGINS_REQUIRED:" + key)
        if any("example.com" in origin for origin in origins):
            fail("HTTPS_ORIGIN_PLACEHOLDER:" + key)
    profiles = [item.strip() for item in values.get("COMPOSE_PROFILES", "").split(",")]
    mtls_required = values.get("AGENT_MTLS_REQUIRED", "false").lower() == "true"
    if "agent-mtls" in profiles and not mtls_required:
        fail("AGENT_MTLS_REQUIRED_FOR_INGRESS_PROFILE")
    if mtls_required:
        if "agent-mtls" not in profiles:
            fail("AGENT_MTLS_PROFILE_REQUIRED")
        subnet = ipaddress.ip_network(
            values.get("AGENT_MTLS_NETWORK_SUBNET", "172.30.250.0/24"), strict=True
        )
        ingress_ip = ipaddress.ip_address(
            values.get("AGENT_MTLS_INGRESS_IP", "172.30.250.2")
        )
        if ingress_ip not in subnet or ingress_ip in {
            subnet.network_address,
            subnet.broadcast_address,
        }:
            fail("AGENT_MTLS_INGRESS_IP_MUST_BE_USABLE_IN_NETWORK_SUBNET")
        for key in ("AGENT_MTLS_SERVER_PEM", "AGENT_MTLS_CLIENT_CA_PEM"):
            path = values.get(key, "")
            if not path or not os.path.isfile(path):
                fail("AGENT_MTLS_CERTIFICATE_FILE_REQUIRED:" + key)


def read_web_port(values: dict[str, str]) -> int:
    raw = os.environ.get("WEB_PORT") or values.get("WEB_PORT", "") or "8080"
    try:
        return int(raw.strip())
    except ValueError:
        fail("WEB_PORT_INVALID")


def compose(*args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(ENV_FILE),
        "-f",
        str(COMPOSE_FILE),
        *args,
    ]


def wait_for_readiness(port: int) -> bool:
    url = f"http://127.0.0.1:{port}/health/ready"
    deadline = time.monotonic() + 30 * 2
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=8) as response:
                if 200 <= response.status < 300:
                    return True
        except urllib.error.URLError:
            pass
        time.sleep(2)
    return False


def main() -> None:
    if not ENV_FILE.is_file():
        fail("CLOUD_ENV_FILE_NOT_FOUND")
    ENV_FILE.chmod(0o600)
    for binary, code in (
        ("docker", "DOCKER_NOT_INSTALLED"),
        ("python3", "PYTHON3_NOT_INSTALLED"),
        ("curl", "CURL_NOT_INSTALLED"),
    ):
        if shutil.which(binary) is None:
            fail(code)
    result = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True
    )
    if result.returncode != 0:
        fail("DOCKER_COMPOSE_V2_REQUIRED")

    values = parse_env(ENV_FILE)
    validate(values)
    web_port = read_web_port(values)

    subprocess.run(compose("config", "--quiet"), check=True)
    subprocess.run(compose("up", "--build", "-d"), check=True)

    if not wait_for_readiness(web_port):
        subprocess.run(
            compose("logs", "--tail=200", "api", "worker", "web"), check=False
        )
        fail("CLOUD_CONTROL_PLANE_HEALTHCHECK_FAILED")

    with urllib.request.urlopen(f"http://127.0.0.1:{web_port}/", timeout=8):
        pass
    subprocess.run(compose("ps"), check=True)
    print("CLOUD_CONTROL_PLANE_DEPLOYED")


if __name__ == "__main__":
    main()
