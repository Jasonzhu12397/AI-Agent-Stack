from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class LeaseElectionError(RuntimeError):
    pass


class KubernetesLeaseElector:
    def __init__(
        self,
        identity: str,
        namespace: str,
        lease_name: str,
        duration_seconds: int = 30,
        request_timeout: float = 3.0,
    ) -> None:
        if not identity or not namespace or not lease_name:
            raise ValueError("Lease identity, namespace and name are required")
        if duration_seconds < 10 or duration_seconds > 120:
            raise ValueError("Lease duration must be between 10 and 120 seconds")
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        if not host:
            raise LeaseElectionError("In-cluster Kubernetes API environment is unavailable")
        self.base_url = f"https://{host}:{port}"
        self.identity = identity
        self.namespace = namespace
        self.lease_name = lease_name
        self.duration_seconds = duration_seconds
        self.request_timeout = request_timeout
        self.token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
        self.ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
        if not self.token_path.is_file() or not self.ca_path.is_file():
            raise LeaseElectionError("Kubernetes service-account credentials are unavailable")
        self.context = ssl.create_default_context(cafile=str(self.ca_path))
        self.collection_url = (
            f"{self.base_url}/apis/coordination.k8s.io/v1/namespaces/"
            f"{self.namespace}/leases"
        )
        self.lease_url = f"{self.collection_url}/{self.lease_name}"

    def _request(self, method: str, url: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        token = self.token_path.read_text(encoding="utf-8").strip()
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request, context=self.context, timeout=self.request_timeout
            ) as response:
                document = json.loads(response.read().decode("utf-8"))
                return response.status, document
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 409, 422):
                try:
                    document = json.loads(exc.read().decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    document = {}
                return exc.code, document
            raise LeaseElectionError(f"Kubernetes Lease API returned HTTP {exc.code}") from exc
        except (OSError, TimeoutError, ValueError) as exc:
            raise LeaseElectionError("Kubernetes Lease API request failed") from exc

    @staticmethod
    def _parse_time(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    def try_acquire_or_renew(self) -> bool:
        status, lease = self._request("GET", self.lease_url)
        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="seconds").replace("+00:00", "Z")
        if status == 404:
            body = {
                "apiVersion": "coordination.k8s.io/v1",
                "kind": "Lease",
                "metadata": {"name": self.lease_name, "namespace": self.namespace},
                "spec": {
                    "holderIdentity": self.identity,
                    "leaseDurationSeconds": self.duration_seconds,
                    "acquireTime": now_text,
                    "renewTime": now_text,
                    "leaseTransitions": 0,
                },
            }
            created, _ = self._request("POST", self.collection_url, body)
            return created == 201
        if status != 200:
            return False
        spec = lease.get("spec", {})
        holder = spec.get("holderIdentity")
        renew_time = self._parse_time(spec.get("renewTime"))
        duration = int(spec.get("leaseDurationSeconds") or self.duration_seconds)
        expired = renew_time is None or now >= renew_time + timedelta(seconds=duration)
        if holder not in (None, self.identity) and not expired:
            return False
        metadata = lease.get("metadata", {})
        body = {
            "apiVersion": "coordination.k8s.io/v1",
            "kind": "Lease",
            "metadata": {
                "name": self.lease_name,
                "namespace": self.namespace,
                "resourceVersion": metadata.get("resourceVersion"),
            },
            "spec": {
                "holderIdentity": self.identity,
                "leaseDurationSeconds": self.duration_seconds,
                "acquireTime": now_text if holder != self.identity else (spec.get("acquireTime") or now_text),
                "renewTime": now_text,
                "leaseTransitions": int(spec.get("leaseTransitions") or 0)
                + (1 if holder not in (None, self.identity) else 0),
            },
        }
        updated, _ = self._request("PUT", self.lease_url, body)
        return updated == 200
