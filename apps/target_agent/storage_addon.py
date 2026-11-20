"""Target Agent installer for the Rook-Ceph storage addon.

The installer is intentionally narrow: it applies vendored Rook CRDs/common/
operator manifests, renders the two storage NetworkAttachmentDefinitions and a
CephCluster from the validated shared contract, waits for Rook readiness, then
creates the requested block/filesystem StorageClasses. It never shells out to
bash, never downloads manifests at runtime and never writes host NIC config.

Network contract:

* control-plane traffic stays on the control network;
* CNF/application data traffic stays on the data or SR-IOV network;
* Ceph public traffic uses the external storage network;
* Ceph replication traffic uses the internal storage network.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from packages.agent_contracts.storage_addon import (
    StorageAddonError,
    plan_storage_addon,
    render_ceph_cluster,
    render_network_attachments,
    render_storage_resources,
    validate_ceph_cluster_ready,
)

FIELD_MANAGER = "ai-agent-target-storage-addon"
PLAN_DIR_ENV = "ROOK_CEPH_MANIFEST_DIR"
DEFAULT_MANIFEST_DIR = Path("/opt/addons/rook-ceph")
REPO_MANIFEST_DIR = (
    Path(__file__).resolve().parents[2] / "deploy" / "offline" / "addons" / "rook-ceph"
)
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
OPERATOR_DEPLOYMENT = "rook-ceph-operator"


class StorageAddonInstallError(RuntimeError):
    def __init__(self, code: str, side_effects: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.side_effects = side_effects


def _manifest_dir(environ: Any) -> Path:
    configured = environ.get(PLAN_DIR_ENV) if environ is not None else None
    if configured:
        return Path(configured)
    if DEFAULT_MANIFEST_DIR.is_dir():
        return DEFAULT_MANIFEST_DIR
    return REPO_MANIFEST_DIR


def _invoke(
    runner: Callable[..., dict[str, Any]],
    arguments: list[str],
    timeout: int,
) -> dict[str, Any]:
    result = runner(arguments, timeout=timeout, max_output_bytes=MAX_OUTPUT_BYTES)
    if not isinstance(result, dict) or result.get("available") is not True:
        raise StorageAddonInstallError("STORAGE_ADDON_KUBERNETES_API_UNAVAILABLE")
    if result.get("returncode") != 0:
        raise StorageAddonInstallError("STORAGE_ADDON_OPERATION_FAILED")
    return result


def _ensure_lease(lease_lost_event: Any, code: str) -> None:
    if lease_lost_event is not None and lease_lost_event.is_set():
        raise StorageAddonInstallError(code)


def _read_manifest(path: Path) -> str:
    if not path.is_file():
        raise StorageAddonInstallError("STORAGE_ADDON_ROOK_MANIFESTS_MISSING")
    text = path.read_text(encoding="utf-8")
    if len(text.encode("utf-8")) > 4 * 1024 * 1024:
        raise StorageAddonInstallError("STORAGE_ADDON_ROOK_MANIFEST_TOO_LARGE")
    return text


def _check_manifest_version(text: str, rook_version: str) -> None:
    expected = f"rook/ceph:{rook_version}"
    if expected not in text:
        raise StorageAddonInstallError("STORAGE_ADDON_ROOK_MANIFEST_VERSION_MISMATCH")


def _apply_file(
    path: Path,
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any,
) -> None:
    _ensure_lease(lease_lost_event, "STORAGE_ADDON_LEASE_LOST_BEFORE_APPLY")
    _invoke(
        runner,
        [
            "apply",
            "--server-side",
            f"--field-manager={FIELD_MANAGER}",
            "-f",
            str(path),
        ],
        900,
    )


def _apply_documents(
    documents: list[dict[str, Any]],
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any,
) -> None:
    serialized = yaml.safe_dump_all(documents, sort_keys=False)
    with tempfile.NamedTemporaryFile(
        mode="w",
        prefix="storage-addon-",
        suffix=".yaml",
        delete=False,
        encoding="utf-8",
    ) as handle:
        handle.write(serialized)
        path = Path(handle.name)
    try:
        os.chmod(path, 0o600)
        _ensure_lease(lease_lost_event, "STORAGE_ADDON_LEASE_LOST_BEFORE_APPLY")
        _invoke(
            runner,
            [
                "apply",
                "--server-side",
                f"--field-manager={FIELD_MANAGER}",
                "--dry-run=server",
                "-f",
                str(path),
            ],
            300,
        )
        _ensure_lease(lease_lost_event, "STORAGE_ADDON_LEASE_LOST_AFTER_DRY_RUN")
        _invoke(
            runner,
            [
                "apply",
                "--server-side",
                f"--field-manager={FIELD_MANAGER}",
                "-f",
                str(path),
            ],
            600,
        )
    finally:
        path.unlink(missing_ok=True)


def _deployment_ready(
    namespace: str,
    runner: Callable[..., dict[str, Any]],
    timeout_seconds: int,
) -> None:
    _invoke(
        runner,
        [
            "rollout",
            "status",
            f"deployment/{OPERATOR_DEPLOYMENT}",
            "--namespace",
            namespace,
            f"--timeout={timeout_seconds}s",
        ],
        timeout_seconds + 10,
    )


def _read_ceph_cluster(
    namespace: str,
    cluster_name: str,
    runner: Callable[..., dict[str, Any]],
) -> dict[str, Any] | None:
    result = runner(
        [
            "get",
            "cephcluster",
            cluster_name,
            "--namespace",
            namespace,
            "-o",
            "json",
        ],
        timeout=60,
        max_output_bytes=MAX_OUTPUT_BYTES,
    )
    if not isinstance(result, dict):
        raise StorageAddonInstallError("STORAGE_ADDON_KUBERNETES_API_UNAVAILABLE")
    if result.get("available") is not True:
        raise StorageAddonInstallError("STORAGE_ADDON_KUBERNETES_API_UNAVAILABLE")
    if result.get("returncode") != 0:
        error = str(result.get("stderr", "")).lower()
        if "notfound" in error or "not found" in error:
            return None
        raise StorageAddonInstallError("STORAGE_ADDON_CEPHCLUSTER_OBSERVE_FAILED")
    try:
        document = json.loads(result.get("stdout", ""))
    except (TypeError, ValueError) as exc:
        raise StorageAddonInstallError(
            "STORAGE_ADDON_CEPHCLUSTER_DOCUMENT_INVALID"
        ) from exc
    if not isinstance(document, dict):
        raise StorageAddonInstallError("STORAGE_ADDON_CEPHCLUSTER_DOCUMENT_INVALID")
    return document


def _wait_ceph_cluster(
    namespace: str,
    cluster_name: str,
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = monotonic() + timeout_seconds
    delay = 10.0
    last_phase = ""
    while monotonic() < deadline:
        _ensure_lease(lease_lost_event, "STORAGE_ADDON_LEASE_LOST_DURING_WAIT")
        document = _read_ceph_cluster(namespace, cluster_name, runner)
        if document is not None:
            status = document.get("status")
            if isinstance(status, dict):
                last_phase = str(status.get("phase", ""))
                if last_phase == "Ready":
                    validate_ceph_cluster_ready(document)
                    return document
            if last_phase in ("Failed", "Error"):
                raise StorageAddonInstallError("STORAGE_ADDON_CEPHCLUSTER_FAILED")
        remaining = max(0.0, deadline - monotonic())
        sleep(min(delay, remaining))
        delay = min(delay * 1.5, 60.0)
    raise StorageAddonInstallError(
        "STORAGE_ADDON_CEPHCLUSTER_READY_TIMEOUT:" + (last_phase or "unknown")
    )


def _storage_class_names(plan: dict[str, Any]) -> list[str]:
    classes = plan["storage_classes"]
    names = []
    if classes["block"]:
        names.append(classes["block_class"])
    if classes["filesystem"]:
        names.append(classes["filesystem_class"])
    return names


def execute_storage_addon(
    value: Any,
    *,
    worker_network_contract: Any,
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    environ: Any = None,
) -> dict[str, Any]:
    """Install and verify the configured storage addon.

    Disabled or absent configuration returns an explicit ``enabled: false``
    evidence record without touching the cluster.
    """

    if value is None:
        return {"storage_addon_enabled": False, "side_effects": False}
    if (
        isinstance(value, dict)
        and value.get("enabled") is True
        and not isinstance(worker_network_contract, dict)
    ):
        raise StorageAddonInstallError(
            "STORAGE_ADDON_WORKER_NETWORK_CONTRACT_REQUIRED"
        )
    try:
        plan = plan_storage_addon(
            value,
            worker_network_contract=worker_network_contract,
        )
    except StorageAddonError as exc:
        raise StorageAddonInstallError(str(exc)) from exc
    if not plan.get("enabled"):
        return {"storage_addon_enabled": False, "side_effects": False}

    manifest_dir = _manifest_dir(environ)
    files = {
        name: manifest_dir / name
        for name in ("crds.yaml", "common.yaml", "operator.yaml")
    }
    manifest_texts = {name: _read_manifest(path) for name, path in files.items()}
    _check_manifest_version(manifest_texts["operator.yaml"], plan["rook_version"])
    plan_hash = hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    side_effects = False
    try:
        if plan["operator_install"]:
            _apply_file(files["crds.yaml"], runner, lease_lost_event)
            side_effects = True
            _apply_file(files["common.yaml"], runner, lease_lost_event)
            _apply_file(files["operator.yaml"], runner, lease_lost_event)
            _invoke(
                runner,
                [
                    "wait",
                    "--for=condition=Established",
                    "crd/cephclusters.ceph.rook.io",
                    "--timeout=180s",
                ],
                190,
            )
        _deployment_ready(
            plan["operator_namespace"],
            runner,
            min(600, plan["timeout_seconds"]),
        )

        _apply_documents(
            render_network_attachments(plan),
            runner,
            lease_lost_event,
        )
        side_effects = True
        _apply_documents(
            [render_ceph_cluster(plan)],
            runner,
            lease_lost_event,
        )
        cluster_document = _wait_ceph_cluster(
            plan["namespace"],
            plan["cluster_name"],
            runner,
            lease_lost_event,
            sleep,
            monotonic,
            plan["timeout_seconds"],
        )
        _apply_documents(
            render_storage_resources(plan),
            runner,
            lease_lost_event,
        )
    except StorageAddonInstallError as exc:
        exc.side_effects = exc.side_effects or side_effects
        raise

    status = cluster_document.get("status") if isinstance(cluster_document, dict) else {}
    ceph_status = status.get("ceph") if isinstance(status, dict) else {}
    return {
        "storage_addon_enabled": True,
        "storage_addon_type": "rook-ceph",
        "storage_addon_version": plan["rook_version"],
        "storage_addon_plan_sha256": plan_hash,
        "storage_addon_namespace": plan["namespace"],
        "storage_addon_ready": True,
        "storage_network_internal": {
            "name": plan["network"]["internal"]["name"],
            "cidr": plan["network"]["internal"]["cidr"],
            "role": "ceph_cluster_replication",
        },
        "storage_network_external": {
            "name": plan["network"]["external"]["name"],
            "cidr": plan["network"]["external"]["cidr"],
            "role": "ceph_public_client",
        },
        "storage_classes": _storage_class_names(plan),
        "ceph_cluster_phase": status.get("phase"),
        "ceph_health": ceph_status.get("health") if isinstance(ceph_status, dict) else None,
        "side_effects": side_effects,
    }
