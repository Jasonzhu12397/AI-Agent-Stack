"""Resolve BMC credentials from Vault for the offline Metal3 tools.

The Rust API owns the production path in `apps/rust-api/src/fleet_boot.rs`. The
operator tools keep an equivalent read-only resolver so a lab host can fetch a
BMC username/password from environment configuration.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx

_SECRET_REF_PATTERN = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9._-]+)*$")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _parse_vault_reference(reference: str) -> tuple[str, str]:
    parsed = urlsplit(reference)
    if (
        parsed.scheme != "vault"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.port
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Vault reference has an invalid format.")
    mount = parsed.netloc
    if not re.fullmatch(r"[A-Za-z0-9_-]+", mount):
        raise ValueError("Vault mount is invalid.")
    path = parsed.path.lstrip("/")
    if not _SECRET_REF_PATTERN.fullmatch(path):
        raise ValueError("Vault reference path is invalid.")
    if any(part in {".", ".."} for part in path.split("/")):
        raise ValueError("Vault reference path is invalid.")
    return mount, path


def _vault_url() -> str:
    address = _env("BMC_VAULT_ADDR").rstrip("/")
    parsed = urlsplit(address)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("Vault address must be an HTTPS origin.")
    return address


def _tenant_scoped_path(path: str, tenant_id: str, project_id: str) -> str:
    tenant = str(tenant_id or "")
    project = str(project_id or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tenant):
        raise ValueError("Vault tenant scope is required.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", project):
        raise ValueError("Vault project scope is required.")
    return f"tenants/{tenant}/projects/{project}/{path}"


def _jwt_claim(payload: dict, claim_path: str) -> object:
    value: object = payload
    for part in claim_path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _client(token: str | None = None) -> httpx.Client:
    headers = {}
    namespace = _env("BMC_VAULT_NAMESPACE")
    if namespace:
        headers["X-Vault-Namespace"] = namespace
    if token:
        headers["X-Vault-Token"] = token
    ca_bundle = _env("BMC_VAULT_CA_BUNDLE")
    timeout_value = float(_env("BMC_VAULT_TIMEOUT_SECONDS", "5"))
    return httpx.Client(
        headers=headers,
        verify=ca_bundle or True,
        timeout=httpx.Timeout(timeout_value, connect=min(3.0, timeout_value)),
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
    )


def _read_kv_v2(client: httpx.Client, mount: str, path: str) -> dict:
    response = client.get(f"{_vault_url()}/v1/{mount}/data/{path}")
    if response.status_code != 200:
        raise RuntimeError("Vault secret lookup failed.")
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise ValueError("Vault returned a non-JSON response.")
    if len(response.content) > 65536:
        raise ValueError("Vault secret response exceeded the size limit.")
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
        raise ValueError("Vault KV v2 response is missing secret data.")
    return data["data"]


def _jwt_token(tenant_id: str, project_id: str) -> str:
    jwt_path = _env("BMC_VAULT_JWT_PATH")
    role = _env("BMC_VAULT_JWT_ROLE")
    if not jwt_path or not role:
        raise RuntimeError("Vault JWT auth is not configured.")
    try:
        jwt = Path(jwt_path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("Vault workload identity token is unavailable.") from exc
    segments = jwt.split(".")
    if len(segments) != 3 or len(jwt) > 16384:
        raise ValueError("Vault workload identity token is invalid.")
    claims = json.loads(base64.urlsafe_b64decode(segments[1] + "=" * (-len(segments[1]) % 4)))
    tenant_claim = _env("BMC_VAULT_JWT_TENANT_CLAIM", "tenant_id")
    project_claim = _env("BMC_VAULT_JWT_PROJECT_CLAIM", "project_id")
    if (
        not isinstance(claims, dict)
        or _jwt_claim(claims, tenant_claim) != str(tenant_id)
        or _jwt_claim(claims, project_claim) != str(project_id)
    ):
        raise PermissionError("Vault workload identity scope does not match the request.")
    auth_mount = _env("BMC_VAULT_JWT_AUTH_MOUNT", "jwt")
    with _client() as client:
        response = client.post(
            f"{_vault_url()}/v1/auth/{auth_mount}/login",
            json={"role": role, "jwt": jwt},
        )
    if response.status_code != 200:
        raise RuntimeError("Vault workload identity login failed.")
    token = response.json().get("auth", {}).get("client_token")
    if not isinstance(token, str) or not token or any(character.isspace() for character in token):
        raise RuntimeError("Vault JWT login did not return a valid client token.")
    return token


def _vault_token(tenant_id: str, project_id: str) -> str:
    resolver_path = _env("BMC_VAULT_TOKEN_RESOLVER")
    if resolver_path:
        from importlib import import_module

        module_name, _, attribute = resolver_path.rpartition(".")
        resolver = getattr(import_module(module_name), attribute)
        token = resolver(tenant_id=str(tenant_id), project_id=str(project_id))
    elif _env("BMC_VAULT_JWT_PATH"):
        token = _jwt_token(tenant_id, project_id)
    else:
        token = _env("BMC_VAULT_TOKEN")
    if not token or any(character.isspace() for character in token):
        raise RuntimeError("Vault authentication returned an invalid token.")
    return token


def resolve_bmc_credentials(
    credential_ref: str,
    trust_bundle_ref: str = "",
    tenant_id: str = "",
    project_id: str = "",
) -> dict[str, str]:
    mount, path = _parse_vault_reference(credential_ref)
    path = _tenant_scoped_path(path, tenant_id, project_id)
    trust_reference = None
    if trust_bundle_ref:
        trust_mount, trust_path = _parse_vault_reference(trust_bundle_ref)
        trust_reference = (trust_mount, _tenant_scoped_path(trust_path, tenant_id, project_id))
    token = _vault_token(tenant_id, project_id)
    with _client(token) as client:
        secret = _read_kv_v2(client, mount, path)
        username = secret.get("username")
        password = secret.get("password")
        if (
            not isinstance(username, str)
            or not username
            or not isinstance(password, str)
            or not password
        ):
            raise ValueError("Vault BMC secret must contain username and password.")
        result = {"username": username, "password": password}
        if trust_reference:
            trust = _read_kv_v2(client, *trust_reference)
            pem = trust.get("pem") or trust.get("certificate")
            if not isinstance(pem, str) or not pem.strip():
                raise ValueError("Vault trust bundle secret must contain PEM data.")
            result["trust_bundle_pem"] = pem
    return result
