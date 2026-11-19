"""Translate a platform cluster request into a Cloud Control deployment."""

from __future__ import annotations

import copy
import dataclasses
import uuid
from typing import Any

from django.conf import settings

from core.cloud_control_client import (
    CloudControlClient,
    CloudControlDispatchError,
)
from tools.metal3_fleet_source import load_platform_fleet


class CloudControlBridgeError(RuntimeError):
    code = "CLOUD_CONTROL_BRIDGE_ERROR"


class CloudControlNotConfigured(CloudControlBridgeError):
    code = "CLOUD_CONTROL_NOT_CONFIGURED"


class FleetInventoryEmpty(CloudControlBridgeError):
    code = "FLEET_INVENTORY_EMPTY"


class FleetInventoryInsufficient(CloudControlBridgeError):
    code = "FLEET_INVENTORY_INSUFFICIENT"


_ID_NAMESPACE = uuid.UUID("7f1c3b52-9d0a-5a2e-8f7b-5f2a6d4c9e11")


def control_identity(
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    eph_node_id: uuid.UUID,
    cluster_name: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Return stable installation and cluster identifiers for one plan."""

    installation_id = uuid.uuid5(
        _ID_NAMESPACE,
        f"installation:{tenant_id}:{project_id}:{eph_node_id}",
    )
    cluster_id = uuid.uuid5(
        _ID_NAMESPACE,
        f"cluster:{tenant_id}:{project_id}:{cluster_name}",
    )
    return installation_id, cluster_id


def build_cloud_control_spec(
    cluster_spec: dict[str, Any],
    *,
    project_id: uuid.UUID,
    cluster_name: str,
) -> dict[str, Any]:
    """Build the spec the control plane persists, including a real fleet.

    The caller may already have planned a fleet; otherwise the platform's own
    AgentMachine inventory is used. No machine facts are invented: an empty or
    incomplete inventory fails closed so the EPH Agent never reasons over a
    placeholder fleet.
    """

    spec = copy.deepcopy(cluster_spec)
    spec["cluster_name"] = cluster_name
    spec.setdefault("namespace", "metal3")
    spec.setdefault("k8s_version", spec.get("kubernetes_version", "v1.37.2"))
    if isinstance(spec.get("control_plane_replicas"), int):
        spec.setdefault("control_plane_count", spec["control_plane_replicas"])
    if isinstance(spec.get("worker_replicas"), int):
        spec.setdefault("worker_replicas", spec["worker_replicas"])

    fleet = spec.get("fleet")
    if not isinstance(fleet, list) or not fleet:
        machines = load_platform_fleet(
            project_id=str(project_id),
            require_credentials=False,
        )
        fleet = [
            dataclasses.asdict(machine.spec)
            for machine in machines
        ]
    if not fleet:
        raise FleetInventoryEmpty()
    if not all(isinstance(item, dict) for item in fleet):
        raise FleetInventoryEmpty()
    control_plane_count = spec.get("control_plane_count")
    worker_replicas = spec.get("worker_replicas")
    if isinstance(control_plane_count, int) and isinstance(worker_replicas, int):
        required = control_plane_count + worker_replicas
        if len(fleet) < required:
            raise FleetInventoryInsufficient(
                f"fleet has {len(fleet)} machine(s), but the request needs {required}"
            )
    spec["fleet"] = fleet
    return spec


def build_cloud_control_client() -> CloudControlClient:
    url = str(getattr(settings, "CLOUD_CONTROL_API_URL", "") or "")
    ca_file = str(getattr(settings, "CLOUD_CONTROL_CA_FILE", "") or "")
    cert_file = str(getattr(settings, "CLOUD_CONTROL_CLIENT_CERT_FILE", "") or "")
    key_file = str(getattr(settings, "CLOUD_CONTROL_CLIENT_KEY_FILE", "") or "")
    if not all((url, ca_file, cert_file, key_file)):
        raise CloudControlNotConfigured()
    return CloudControlClient(url, ca_file, cert_file, key_file)


def dispatch_deployment(
    *,
    deployment_id: uuid.UUID,
    installation_id: uuid.UUID,
    cluster_id: uuid.UUID,
    spec: dict[str, Any],
    client: CloudControlClient | None = None,
) -> dict[str, Any]:
    if client is None:
        client = build_cloud_control_client()
    try:
        return client.create_deployment(
            deployment_id=deployment_id,
            installation_id=installation_id,
            cluster_id=cluster_id,
            spec=spec,
        )
    except CloudControlDispatchError as exc:
        raise CloudControlBridgeError(str(exc)) from exc
