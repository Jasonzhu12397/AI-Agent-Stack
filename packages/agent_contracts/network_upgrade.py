"""Network plugin upgrade and rollback request contracts.

An upgrade is an approved, exact bundle replacement. The Target Agent captures
every object the bundle touches in a revision ConfigMap before it applies the
new documents; rollback replays that captured bundle. Both directions only act
on the enrolled target cluster.
"""

from __future__ import annotations

import re
from typing import Any

UPGRADE_ACTION = "target.network.upgrade"
ROLLBACK_ACTION = "target.network.rollback"
REVISIONS_ACTION = "target.network.revisions"
PLUGINS = ("calico", "multus", "ovs", "sriov", "dpdk")
EXECUTION_MODES = ("simulation", "execute")
MAX_DOCUMENT_CHARS = 60000
_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
_VERSION = re.compile(r"v?[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.]+)?")
_REVISION = re.compile(r"[0-9]{8}T[0-9]{6}Z(?:-[0-9]+)?")


class NetworkUpgradeError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 63 or not _NAME.fullmatch(value):
        raise NetworkUpgradeError(
            "NETWORK_UPGRADE_REQUEST_INVALID", f"{field} must be a Kubernetes name."
        )
    return value


def _mode(payload: dict[str, Any]) -> str:
    mode = payload.get("execution_mode")
    if mode not in EXECUTION_MODES:
        raise NetworkUpgradeError(
            "NETWORK_UPGRADE_REQUEST_INVALID", "execution_mode must be simulation or execute."
        )
    return mode


def _common(payload: Any, cluster_name: str | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise NetworkUpgradeError("NETWORK_UPGRADE_REQUEST_INVALID", "payload must be an object.")
    target = _name(payload.get("target_cluster_name"), "target_cluster_name")
    if cluster_name and target != cluster_name:
        raise NetworkUpgradeError(
            "NETWORK_UPGRADE_CLUSTER_NOT_ENROLLED",
            "The request targets a cluster this Agent is not enrolled for.",
        )
    plugin = payload.get("plugin")
    if plugin not in PLUGINS:
        raise NetworkUpgradeError(
            "NETWORK_UPGRADE_REQUEST_INVALID", "plugin must be calico, multus, ovs, sriov or dpdk."
        )
    namespace = _name(payload.get("target_namespace") or "default", "target_namespace")
    reason = payload.get("reason") or ""
    if not isinstance(reason, str) or len(reason) > 300:
        raise NetworkUpgradeError("NETWORK_UPGRADE_REQUEST_INVALID", "reason is too long.")
    return {
        "target_cluster_name": target,
        "target_namespace": namespace,
        "plugin": plugin,
        "execution_mode": _mode(payload),
        "reason": reason,
    }


def validate_upgrade_request(payload: Any, *, cluster_name: str | None) -> dict[str, Any]:
    request = _common(payload, cluster_name)
    target_version = payload.get("target_version")
    if not isinstance(target_version, str) or not _VERSION.fullmatch(target_version):
        raise NetworkUpgradeError(
            "NETWORK_UPGRADE_REQUEST_INVALID", "target_version must look like 1.14.2."
        )
    documents = payload.get("documents")
    if not isinstance(documents, str) or not documents.strip():
        raise NetworkUpgradeError(
            "NETWORK_UPGRADE_REQUEST_INVALID", "documents must contain at least one YAML object."
        )
    if len(documents) > MAX_DOCUMENT_CHARS:
        raise NetworkUpgradeError("NETWORK_UPGRADE_REQUEST_INVALID", "documents are too large.")
    return {
        **request,
        "target_version": target_version,
        "documents": documents,
    }


def validate_revisions_request(payload: Any, *, cluster_name: str | None) -> dict[str, Any]:
    if isinstance(payload, dict):
        payload = {**payload, "execution_mode": "simulation"}
    request = _common(payload, cluster_name)
    return {
        "target_cluster_name": request["target_cluster_name"],
        "target_namespace": request["target_namespace"],
        "plugin": request["plugin"],
    }


def validate_rollback_request(payload: Any, *, cluster_name: str | None) -> dict[str, Any]:
    request = _common(payload, cluster_name)
    revision = payload.get("revision")
    if revision is not None and (
        not isinstance(revision, str) or not _REVISION.fullmatch(revision)
    ):
        raise NetworkUpgradeError(
            "NETWORK_UPGRADE_REQUEST_INVALID",
            "revision must be a captured id such as 20261126T000000Z.",
        )
    return {**request, "revision": revision}


def upgrade_plan(
    request: dict[str, Any],
    *,
    from_version: str | None,
    objects: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "action": UPGRADE_ACTION,
        "cluster_name": request["target_cluster_name"],
        "namespace": request["target_namespace"],
        "plugin": request["plugin"],
        "from_version": from_version,
        "target_version": request["target_version"],
        "objects": objects,
        "object_count": len(objects),
        "execution_mode": request["execution_mode"],
        "approval_required": True,
        "upgrade_simulated": True,
        "upgrade_performed": False,
        "side_effects": False,
    }


def rollback_plan(
    request: dict[str, Any], *, revision: str, previous_version: str | None
) -> dict[str, Any]:
    return {
        "action": ROLLBACK_ACTION,
        "cluster_name": request["target_cluster_name"],
        "namespace": request["target_namespace"],
        "plugin": request["plugin"],
        "revision": revision,
        "previous_version": previous_version,
        "execution_mode": request["execution_mode"],
        "approval_required": True,
        "rollback_simulated": True,
        "rollback_performed": False,
        "side_effects": False,
    }
