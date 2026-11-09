# ruff: noqa: I001
import json
import re
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.utils.module_loading import import_string


_SECRET_REF_PATTERN = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9._-]+)*$")


def _parse_vault_reference(reference):
    if not isinstance(reference, str):
        raise ValueError("Vault reference must be a string.")
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


def _vault_url():
    address = getattr(settings, "BMC_VAULT_ADDR", "").rstrip("/")
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


def _read_kv_v2(client, address, mount, path, headers):
    url = f"{address}/v1/{mount}/data/{path}"
    with client.stream("GET", url, headers=headers) as response:
        if response.status_code != 200:
            raise RuntimeError("Vault secret lookup failed.")
        content_type = (
            response.headers.get("content-type", "")
            .split(";", 1)[0]
            .strip()
            .lower()
        )
        if content_type != "application/json":
            raise ValueError("Vault returned a non-JSON response.")
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > 65536:
                raise ValueError("Vault secret response exceeded the size limit.")
        try:
            payload = json.loads(body)
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise ValueError("Vault returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Vault returned an invalid secret document.")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
        raise ValueError("Vault KV v2 response is missing secret data.")
    return data["data"]


def _tenant_scoped_path(path, tenant_id, project_id):
    tenant = str(tenant_id or "")
    project = str(project_id or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tenant):
        raise ValueError("Vault tenant scope is required.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", project):
        raise ValueError("Vault project scope is required.")
    return f"tenants/{tenant}/projects/{project}/{path}"


def resolve_bmc_credentials(
    credential_ref,
    trust_bundle_ref="",
    tenant_id="",
    project_id="",
):
    _tenant_scoped_path("validation", tenant_id, project_id)
    resolver_path = getattr(settings, "BMC_VAULT_TOKEN_RESOLVER", "")
    if resolver_path:
        token_resolver = import_string(resolver_path)
        token = token_resolver(tenant_id=str(tenant_id), project_id=str(project_id))
        if (
            not isinstance(token, str)
            or not token
            or token.strip() != token
            or any(character.isspace() for character in token)
        ):
            raise RuntimeError("Scoped Vault token resolver returned an invalid token.")
    else:
        token = getattr(settings, "BMC_VAULT_TOKEN", "")
        if not isinstance(token, str) or not token:
            raise RuntimeError("Vault authentication is not configured.")
    address = _vault_url()
    mount, path = _parse_vault_reference(credential_ref)
    path = _tenant_scoped_path(path, tenant_id, project_id)
    headers = {"X-Vault-Token": token}
    namespace = getattr(settings, "BMC_VAULT_NAMESPACE", "")
    if namespace:
        headers["X-Vault-Namespace"] = namespace
    ca_bundle = getattr(settings, "BMC_VAULT_CA_BUNDLE", "")
    verify = ca_bundle if ca_bundle else True
    timeout_value = getattr(settings, "BMC_VAULT_TIMEOUT_SECONDS", 5)
    timeout = httpx.Timeout(float(timeout_value), connect=min(3.0, float(timeout_value)))
    with httpx.Client(
        verify=verify,
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
    ) as client:
        secret = _read_kv_v2(client, address, mount, path, headers)
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
        if trust_bundle_ref:
            trust_mount, trust_path = _parse_vault_reference(trust_bundle_ref)
            trust_path = _tenant_scoped_path(trust_path, tenant_id, project_id)
            trust_secret = _read_kv_v2(
                client, address, trust_mount, trust_path, headers
            )
            pem = trust_secret.get("pem") or trust_secret.get("certificate")
            if not isinstance(pem, str) or not pem.strip():
                raise ValueError("Vault trust bundle secret must contain PEM data.")
            result["trust_bundle_pem"] = pem
    return result
