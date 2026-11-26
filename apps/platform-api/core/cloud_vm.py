"""Provider-neutral cloud VM orchestration with an OpenStack Nova adapter.

Provider credentials come from the deployment operator's environment, never
from a tenant request. That keeps a console user from turning the platform into
an SSRF proxy: the only endpoints called are the ones the operator exported.
The API surface here is deliberately small - list, create, delete and a power
action - because those are the four operations a Day-2 flow needs.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import ssl
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from django.utils import timezone

PROVIDER_ENV = "CLOUD_VM_PROVIDERS"
ALLOW_INSECURE_ENV = "CLOUD_VM_ALLOW_INSECURE_TLS"
PROVIDER_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_PROVIDER_MESSAGE = 240
TOKEN_EXPIRY_SKEW = dt.timedelta(seconds=60)

POWER_ACTIONS = {
    "start": ("os-start", None),
    "stop": ("os-stop", None),
    "reboot": ("reboot", "SOFT"),
    "hard-reboot": ("reboot", "HARD"),
}


class CloudVmError(Exception):
    """A failure that maps to the shared API error envelope."""

    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _provider_error(code: str, message: str, http_status: int = 502) -> CloudVmError:
    return CloudVmError(code, message[:MAX_PROVIDER_MESSAGE], http_status)


def _https_url(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 900:
        raise CloudVmError("CLOUD_VM_PROVIDER_INVALID", f"{field} must be an HTTPS URL.")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise CloudVmError("CLOUD_VM_PROVIDER_INVALID", f"{field} is malformed.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.query
    ):
        raise CloudVmError(
            "CLOUD_VM_PROVIDER_INVALID",
            f"{field} must be HTTPS without credentials, query or fragment.",
        )
    return value.rstrip("/")


def _required_text(value: Any, field: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CloudVmError("CLOUD_VM_PROVIDER_INVALID", f"{field} is required.")
    text = value.strip()
    if len(text) > limit:
        raise CloudVmError("CLOUD_VM_PROVIDER_INVALID", f"{field} is too long.")
    return text


def load_provider_configs() -> dict[str, dict[str, Any]]:
    """Read and validate what the operator exported.

    Every entry is validated on every request so a broken deployment fails loudly
    instead of surfacing as an opaque provider timeout later.
    """

    raw = os.environ.get(PROVIDER_ENV, "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CloudVmError(
            "CLOUD_VM_PROVIDER_CONFIG_INVALID",
            f"{PROVIDER_ENV} is not valid JSON.",
            503,
        ) from exc
    if isinstance(payload, dict):
        entries = [{"key": key, **value} for key, value in payload.items()]
    elif isinstance(payload, list):
        entries = payload
    else:
        raise CloudVmError(
            "CLOUD_VM_PROVIDER_CONFIG_INVALID",
            f"{PROVIDER_ENV} must be a JSON array or object.",
            503,
        )
    providers: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise CloudVmError(
                "CLOUD_VM_PROVIDER_CONFIG_INVALID", "Each provider entry must be an object.", 503
            )
        key = _required_text(entry.get("key"), "provider key", 64)
        if not PROVIDER_KEY_PATTERN.fullmatch(key) or key in providers:
            raise CloudVmError(
                "CLOUD_VM_PROVIDER_CONFIG_INVALID",
                "Provider keys must be unique lowercase identifiers.",
                503,
            )
        if entry.get("type") != "openstack":
            raise CloudVmError(
                "CLOUD_VM_PROVIDER_CONFIG_INVALID",
                f"Provider {key} has an unsupported type.",
                503,
            )
        verify_tls = entry.get("verify_tls", True)
        if not isinstance(verify_tls, bool):
            raise CloudVmError(
                "CLOUD_VM_PROVIDER_CONFIG_INVALID",
                f"Provider {key} verify_tls must be a boolean.",
                503,
            )
        if not verify_tls and os.environ.get(ALLOW_INSECURE_ENV) != "1":
            raise CloudVmError(
                "CLOUD_VM_PROVIDER_CONFIG_INVALID",
                f"Provider {key} disables TLS verification without {ALLOW_INSECURE_ENV}=1.",
                503,
            )
        config: dict[str, Any] = {
            "key": key,
            "type": "openstack",
            "display_name": _required_text(entry.get("display_name") or key, "display_name", 120),
            "auth_url": _https_url(entry.get("auth_url"), "auth_url"),
            "username": _required_text(entry.get("username"), "username", 256),
            "password": _required_text(entry.get("password"), "password", 512),
            "project_name": _required_text(entry.get("project_name"), "project_name", 256),
            "domain_name": _required_text(
                entry.get("domain_name") or "Default", "domain_name", 256
            ),
            "region": (entry.get("region") or "").strip(),
            "verify_tls": verify_tls,
            "timeout": entry.get("timeout", 20),
        }
        if not isinstance(config["timeout"], int) or not 3 <= config["timeout"] <= 120:
            raise CloudVmError(
                "CLOUD_VM_PROVIDER_CONFIG_INVALID",
                f"Provider {key} timeout must be an integer between 3 and 120.",
                503,
            )
        if entry.get("compute_url"):
            config["compute_url"] = _https_url(entry["compute_url"], "compute_url")
        providers[key] = config
    return providers


def public_providers() -> list[dict[str, Any]]:
    """Provider descriptors that are safe to render in the console."""

    return [
        {
            "key": item["key"],
            "type": item["type"],
            "display_name": item["display_name"],
            "region": item["region"],
        }
        for item in load_provider_configs().values()
    ]


def build_provider(key: str) -> OpenStackProvider:
    configs = load_provider_configs()
    config = configs.get(key)
    if config is None:
        raise CloudVmError(
            "CLOUD_VM_PROVIDER_UNKNOWN",
            f"Cloud provider {key!r} is not configured.",
            404,
        )
    return OpenStackProvider(config)


def _provider_message(response: Any) -> str:
    try:
        payload = response.json()
    except ValueError:
        return str(getattr(response, "text", ""))[:MAX_PROVIDER_MESSAGE] or (
            "Provider returned no message."
        )
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        for key in ("message", "detail", "faultstring"):
            if payload.get(key):
                return str(payload[key])
    return str(payload)[:MAX_PROVIDER_MESSAGE]


def _flatten_addresses(addresses: Any) -> str | None:
    if not isinstance(addresses, dict):
        return None
    candidates: list[tuple[int, str]] = []
    for entries in addresses.values():
        if not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, dict) or item.get("version") != 4:
                continue
            address = item.get("addr")
            if not isinstance(address, str):
                continue
            rank = 0 if item.get("OS-EXT-IPS:type") == "fixed" else 1
            candidates.append((rank, address))
    if not candidates:
        return None
    return sorted(candidates)[0][1]


def normalize_server(server: Any) -> dict[str, Any]:
    """Reduce a Nova payload to the fields the console stores."""

    if not isinstance(server, dict) or not server.get("id"):
        raise _provider_error("CLOUD_VM_PROVIDER_RESPONSE_INVALID", "Server payload is malformed.")
    metadata: dict[str, Any] = {}
    flavor = server.get("flavor")
    image = server.get("image")
    if isinstance(flavor, dict) and flavor.get("id"):
        metadata["flavor_id"] = flavor["id"]
    if isinstance(image, dict) and image.get("id"):
        metadata["image_id"] = image["id"]
    for key in ("created", "updated", "accessIPv4", "key_name"):
        if server.get(key):
            metadata[key] = server[key]
    return {
        "server_id": str(server["id"]),
        "name": str(server.get("name") or server["id"]),
        "status": str(server.get("status") or "UNKNOWN")[:32].upper(),
        "ip_address": _flatten_addresses(server.get("addresses")),
        "metadata": metadata,
    }


class _HttpResponse:
    """The small slice of an HTTP response this client needs."""

    def __init__(self, status_code: int, body: bytes, headers: Any):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    @property
    def text(self) -> str:
        try:
            return self._body.decode("utf-8", errors="replace")
        except AttributeError:
            return ""

    def json(self):
        return json.loads(self.text)


class _UrllibSession:
    """Minimal session built on the standard library.

    Keeping this in-tree avoids adding an HTTP dependency to the platform API
    just for one provider adapter, and the explicit TLS context makes
    ``verify_tls`` a single decision instead of an ambient one.
    """

    def request(self, method: str, url: str, **kwargs) -> _HttpResponse:
        body = kwargs.get("json")
        headers = dict(kwargs.get("headers") or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        verify = kwargs.get("verify", True)
        context = None if verify else ssl._create_unverified_context()
        timeout = kwargs.get("timeout", 20)
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                return _HttpResponse(response.status, response.read(), dict(response.headers))
        except urllib.error.HTTPError as exc:
            return _HttpResponse(exc.code, exc.read(), dict(exc.headers or {}))
        except (urllib.error.URLError, TimeoutError) as exc:
            raise CloudVmError(
                "CLOUD_VM_PROVIDER_UNAVAILABLE",
                f"Cloud provider is unreachable ({type(exc).__name__}).",
                503,
            ) from exc

    def close(self):
        pass


class OpenStackProvider:
    """Minimal Nova v2.1 client with Keystone password authentication."""

    def __init__(self, config: dict[str, Any], *, session: Any | None = None):
        self.config = config
        self.key = config["key"]
        self._session = session or _UrllibSession()
        self._token: str | None = None
        self._token_expires_at: dt.datetime | None = None
        self._compute_url: str | None = None

    @property
    def timeout(self) -> int:
        return int(self.config["timeout"])

    @property
    def verify_tls(self) -> bool:
        return bool(self.config["verify_tls"])

    def close(self) -> None:
        self._session.close()

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200, 201, 202, 204),
    ) -> Any:
        try:
            response = self._session.request(
                method,
                url,
                json=json_body,
                headers=headers,
                timeout=self.timeout,
                verify=self.verify_tls,
            )
        except CloudVmError:
            raise
        except OSError as exc:
            raise _provider_error(
                "CLOUD_VM_PROVIDER_UNAVAILABLE",
                f"Cloud provider {self.key} is unreachable ({type(exc).__name__}).",
                503,
            ) from exc
        if response.status_code not in expected:
            message = _provider_message(response)
            if response.status_code == 404:
                raise _provider_error("CLOUD_VM_RESOURCE_NOT_FOUND", message, 404)
            if response.status_code == 409:
                raise _provider_error("CLOUD_VM_CONFLICT", message, 409)
            if response.status_code in (401, 403):
                raise _provider_error("CLOUD_VM_PROVIDER_UNAUTHORIZED", message, 502)
            raise _provider_error(
                "CLOUD_VM_PROVIDER_ERROR",
                f"Cloud provider {self.key} returned HTTP {response.status_code}: {message}",
                502,
            )
        return response

    def _authenticate(self) -> str:
        if self._token and self._token_expires_at:
            if timezone.now() < self._token_expires_at:
                return self._token
        payload = {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name": self.config["username"],
                            "domain": {"name": self.config["domain_name"]},
                            "password": self.config["password"],
                        }
                    },
                },
                "scope": {
                    "project": {
                        "name": self.config["project_name"],
                        "domain": {"name": self.config["domain_name"]},
                    }
                },
            }
        }
        response = self._request(
            "POST",
            f"{self.config['auth_url']}/auth/tokens",
            json_body=payload,
        )
        token = response.headers.get("X-Subject-Token")
        if not token:
            raise _provider_error(
                "CLOUD_VM_PROVIDER_RESPONSE_INVALID", "Keystone did not return a subject token."
            )
        expires_at = None
        try:
            body = response.json()
        except ValueError:
            body = {}
        raw_expiry = (body.get("token") or {}).get("expires_at") if isinstance(body, dict) else None
        if isinstance(raw_expiry, str):
            try:
                expires_at = dt.datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=dt.UTC)
            except ValueError:
                expires_at = None
        if expires_at is None:
            expires_at = timezone.now() + dt.timedelta(minutes=15)
        self._token = token
        self._token_expires_at = expires_at - TOKEN_EXPIRY_SKEW
        self._compute_url = self._catalog_compute_url(body)
        return token

    def _catalog_compute_url(self, body: Any) -> str | None:
        if self.config.get("compute_url"):
            return self.config["compute_url"]
        catalog = (body.get("token") or {}).get("catalog") if isinstance(body, dict) else None
        if not isinstance(catalog, list):
            return None
        wanted_region = self.config.get("region") or ""
        for service in catalog:
            if not isinstance(service, dict) or service.get("type") != "compute":
                continue
            endpoints = service.get("endpoints")
            if not isinstance(endpoints, list):
                continue
            for endpoint in endpoints:
                if not isinstance(endpoint, dict) or endpoint.get("interface") != "public":
                    continue
                region = endpoint.get("region_id") or endpoint.get("region") or ""
                if wanted_region and region != wanted_region:
                    continue
                url = endpoint.get("url")
                if isinstance(url, str) and url.startswith("https://"):
                    return url.rstrip("/")
        return None

    def _compute(self) -> tuple[str, dict[str, str]]:
        token = self._authenticate()
        if not self._compute_url:
            raise _provider_error(
                "CLOUD_VM_PROVIDER_RESPONSE_INVALID",
                f"Provider {self.key} catalog has no public compute endpoint.",
            )
        base = self._compute_url
        if not re.search(r"/v[0-9.]+$", base):
            base = f"{base}/v2.1"
        return base, {"X-Auth-Token": token, "Accept": "application/json"}

    def list_servers(self) -> list[dict[str, Any]]:
        base, headers = self._compute()
        response = self._request("GET", f"{base}/servers/detail", headers=headers)
        try:
            payload = response.json()
        except ValueError as exc:
            raise _provider_error(
                "CLOUD_VM_PROVIDER_RESPONSE_INVALID", "Nova returned non-JSON server data."
            ) from exc
        servers = payload.get("servers") if isinstance(payload, dict) else None
        if not isinstance(servers, list):
            raise _provider_error(
                "CLOUD_VM_PROVIDER_RESPONSE_INVALID", "Nova server list is malformed."
            )
        return [normalize_server(item) for item in servers]

    def create_server(
        self,
        *,
        name: str,
        flavor_ref: str,
        image_ref: str,
        network_ids: list[str],
    ) -> dict[str, Any]:
        base, headers = self._compute()
        body = {
            "server": {
                "name": name,
                "flavorRef": flavor_ref,
                "imageRef": image_ref,
                "networks": [{"uuid": network_id} for network_id in network_ids],
            }
        }
        response = self._request("POST", f"{base}/servers", json_body=body, headers=headers)
        try:
            payload = response.json()
        except ValueError as exc:
            raise _provider_error(
                "CLOUD_VM_PROVIDER_RESPONSE_INVALID", "Nova returned non-JSON create data."
            ) from exc
        server = payload.get("server") if isinstance(payload, dict) else None
        if not isinstance(server, dict):
            raise _provider_error(
                "CLOUD_VM_PROVIDER_RESPONSE_INVALID", "Nova create response is malformed."
            )
        normalized = normalize_server(server)
        if normalized["ip_address"] is None:
            normalized["ip_address"] = _flatten_addresses(server.get("addresses"))
        return normalized

    def delete_server(self, server_id: str) -> None:
        base, headers = self._compute()
        self._request("DELETE", f"{base}/servers/{server_id}", headers=headers)

    def server_action(self, server_id: str, action: str) -> None:
        base, headers = self._compute()
        if action not in POWER_ACTIONS:
            raise CloudVmError("CLOUD_VM_ACTION_INVALID", f"Unsupported action {action}.")
        name, reboot_type = POWER_ACTIONS[action]
        body: dict[str, Any] = {name: reboot_type} if reboot_type else {name: None}
        self._request(
            "POST",
            f"{base}/servers/{server_id}/action",
            json_body=body,
            headers=headers,
        )
