"""Apply and roll back an exact network plugin bundle on the target cluster.

An upgrade first captures every object in the bundle - existing documents and
objects that do not exist yet - in a revision ConfigMap. Only after the history
write succeeds does it apply the new bundle. Rollback replays that captured
bundle: existing objects are re-applied, and objects the upgrade created are
deleted. No command assumes a previous revision it never wrote.
"""

from __future__ import annotations

import copy
import json
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from packages.agent_contracts.network_upgrade import (
    REVISIONS_ACTION,
    UPGRADE_ACTION,
    NetworkUpgradeError,
    rollback_plan,
    upgrade_plan,
    validate_revisions_request,
    validate_rollback_request,
    validate_upgrade_request,
)

MANAGED_BY_LABEL = "ai-agent-stack.io/managed-by"
PLUGIN_ANNOTATION = "ai-agent-stack.io/network-plugin"
VERSION_ANNOTATION = "ai-agent-stack.io/network-version"
CLUSTER_SCOPED_KINDS = {
    "ClusterRole",
    "ClusterRoleBinding",
    "CustomResourceDefinition",
    "MutatingWebhookConfiguration",
    "ValidatingWebhookConfiguration",
    "PriorityClass",
    "APIService",
}
WORKLOAD_KINDS = {"Deployment", "DaemonSet", "StatefulSet"}
FORBIDDEN_KINDS = {"Secret"}
FIELD_MANAGER = "ai-agent-target-agent"
MAX_BUNDLE_BYTES = 900 * 1024
WAIT_ATTEMPTS = 40
WAIT_INTERVAL_SECONDS = 3


class NetworkUpgradeExecutionError(NetworkUpgradeError):
    def __init__(self, code: str, detail: str, side_effects: bool = False) -> None:
        super().__init__(code, detail)
        self.side_effects = side_effects


def _run(
    runner: Callable[..., dict[str, Any]],
    arguments: list[str],
    *,
    lease_lost_event: Any = None,
    timeout: int = 120,
) -> dict[str, Any]:
    if lease_lost_event is not None and lease_lost_event.is_set():
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_COMMAND_LEASE_LOST", "The command lease was lost."
        )
    result = runner(arguments, timeout=timeout, max_output_bytes=4 * 1024 * 1024)
    if not isinstance(result, dict):
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_KUBECTL_FAILED", "The Kubernetes runner returned no result."
        )
    if result.get("available") is True and result.get("returncode") == 0:
        return result
    error = str(result.get("error") or result.get("stderr") or "")
    if (
        "notfound" in error.lower()
        or "not found" in error.lower()
        or result.get("returncode") == 404
    ):
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_RESOURCE_NOT_FOUND", "The requested Kubernetes object does not exist."
        )
    raise NetworkUpgradeExecutionError(
        "NETWORK_UPGRADE_KUBECTL_FAILED", f"kubectl failed: {error[:200]}"
    )


def _identity(document: dict[str, Any]) -> tuple[str, str, str]:
    kind = document.get("kind")
    metadata = document.get("metadata")
    if not isinstance(kind, str) or not kind or not isinstance(metadata, dict):
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_BUNDLE_INVALID", "Every object needs kind and metadata."
        )
    name = metadata.get("name")
    if not isinstance(name, str) or not name:
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_BUNDLE_INVALID", "Every object needs metadata.name."
        )
    namespace = metadata.get("namespace") or ""
    return kind, namespace, name


def _annotate(document: dict[str, Any], plugin: str, version: str) -> None:
    metadata = document["metadata"]
    annotations = metadata.get("annotations")
    if annotations is None:
        annotations = {}
    if not isinstance(annotations, dict):
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_BUNDLE_INVALID", "metadata.annotations must be an object."
        )
    annotations[MANAGED_BY_LABEL] = "target-agent"
    annotations[PLUGIN_ANNOTATION] = plugin
    annotations[VERSION_ANNOTATION] = version
    metadata["annotations"] = annotations


def _bundle(raw: str, *, namespace: str, plugin: str, version: str) -> list[dict[str, Any]]:
    try:
        parsed = list(yaml.safe_load_all(raw))
    except yaml.YAMLError as exc:
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_BUNDLE_INVALID", "documents are not valid YAML."
        ) from exc
    documents: list[dict[str, Any]] = []
    for document in parsed:
        if document is None:
            continue
        if not isinstance(document, dict):
            raise NetworkUpgradeExecutionError(
                "NETWORK_UPGRADE_BUNDLE_INVALID", "Every YAML document must be an object."
            )
        _identity(document)
        kind = document["kind"]
        if kind in FORBIDDEN_KINDS:
            raise NetworkUpgradeExecutionError(
                "NETWORK_UPGRADE_BUNDLE_FORBIDDEN_KIND", "Secrets cannot travel in a bundle."
            )
        metadata = document["metadata"]
        if kind in CLUSTER_SCOPED_KINDS:
            metadata.pop("namespace", None)
        else:
            metadata["namespace"] = namespace
        _annotate(document, plugin, version)
        documents.append(document)
    if not documents:
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_BUNDLE_INVALID", "The bundle contains no Kubernetes object."
        )
    identities = [_identity(document) for document in documents]
    if len(set(identities)) != len(identities):
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_BUNDLE_INVALID", "The bundle repeats an object identity."
        )
    if not any(document["kind"] in WORKLOAD_KINDS for document in documents):
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_BUNDLE_INVALID", "The bundle needs a workload object."
        )
    return documents


def _sanitize(document: dict[str, Any]) -> dict[str, Any]:
    previous = copy.deepcopy(document)
    metadata = previous.get("metadata")
    if isinstance(metadata, dict):
        for key in (
            "managedFields",
            "resourceVersion",
            "uid",
            "creationTimestamp",
            "generation",
            "selfLink",
            "ownerReferences",
        ):
            metadata.pop(key, None)
    previous.pop("status", None)
    return previous


def _get(
    runner,
    kind: str,
    name: str,
    namespace: str,
    lease_lost_event,
) -> dict[str, Any] | None:
    arguments = ["get", kind, name]
    if namespace:
        arguments += ["--namespace", namespace]
    arguments += ["-o", "json"]
    try:
        result = _run(runner, arguments, lease_lost_event=lease_lost_event, timeout=60)
    except NetworkUpgradeExecutionError as exc:
        if exc.code == "NETWORK_UPGRADE_RESOURCE_NOT_FOUND":
            return None
        raise
    try:
        document = json.loads(result.get("stdout", ""))
    except (TypeError, ValueError) as exc:
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_OBJECT_JSON_INVALID", "kubectl returned invalid JSON."
        ) from exc
    if not isinstance(document, dict):
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_OBJECT_JSON_INVALID", "kubectl returned an invalid object."
        )
    return document


def _apply(
    runner,
    documents: list[dict[str, Any]],
    lease_lost_event,
    *,
    dry_run: bool,
) -> None:
    serialized = yaml.safe_dump_all(documents, sort_keys=False)
    encoded = serialized.encode("utf-8")
    if len(encoded) > MAX_BUNDLE_BYTES:
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_BUNDLE_TOO_LARGE", "The rendered bundle is too large."
        )
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", encoding="utf-8", delete=False
        ) as handle:
            handle.write(serialized)
            path = Path(handle.name)
        arguments = [
            "apply",
            "--server-side",
            f"--field-manager={FIELD_MANAGER}",
        ]
        if dry_run:
            arguments.append("--dry-run=server")
        arguments += ["-f", str(path)]
        _run(runner, arguments, lease_lost_event=lease_lost_event, timeout=300)
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def _delete(runner, kind: str, name: str, namespace: str, lease_lost_event) -> None:
    arguments = ["delete", kind, name]
    if namespace:
        arguments += ["--namespace", namespace]
    arguments += ["--ignore-not-found=false", "--wait=false"]
    _run(runner, arguments, lease_lost_event=lease_lost_event, timeout=120)


def _workload_ready(document: dict[str, Any]) -> bool:
    status = document.get("status")
    spec = document.get("spec")
    if not isinstance(status, dict) or not isinstance(spec, dict):
        return False
    if document.get("kind") == "DaemonSet":
        desired = status.get("desiredNumberScheduled")
        ready = status.get("numberReady")
        available = status.get("numberAvailable")
    else:
        desired = spec.get("replicas")
        ready = status.get("readyReplicas")
        available = status.get("availableReplicas")
    if not isinstance(desired, int) or isinstance(desired, bool) or desired < 1:
        return False
    return (
        isinstance(ready, int)
        and not isinstance(ready, bool)
        and ready >= desired
        and isinstance(available, int)
        and not isinstance(available, bool)
        and available >= desired
    )


def _split_crds(
    documents: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split CRDs from the objects that depend on them.

    A custom resource in the same bundle cannot be server-side dry-run before
    the CRD exists in the API server. Approved CRDs are therefore applied first,
    then the remaining objects are dry-run against the real discovery data.
    """

    crds = [
        document for document in documents if document.get("kind") == "CustomResourceDefinition"
    ]
    others = [
        document for document in documents if document.get("kind") != "CustomResourceDefinition"
    ]
    return crds, others


def _wait_workloads(runner, documents: list[dict[str, Any]], lease_lost_event) -> None:
    pending = {
        _identity(document) for document in documents if document.get("kind") in WORKLOAD_KINDS
    }
    for attempt in range(WAIT_ATTEMPTS):
        if lease_lost_event is not None and lease_lost_event.is_set():
            raise NetworkUpgradeExecutionError(
                "NETWORK_UPGRADE_COMMAND_LEASE_LOST",
                "The command lease was lost after apply.",
                side_effects=True,
            )
        for kind, namespace, name in tuple(pending):
            observed = _get(runner, kind, name, namespace, lease_lost_event)
            if observed is not None and _workload_ready(observed):
                pending.remove((kind, namespace, name))
        if not pending:
            return
        if attempt < WAIT_ATTEMPTS - 1:
            time.sleep(WAIT_INTERVAL_SECONDS)
    raise NetworkUpgradeExecutionError(
        "NETWORK_UPGRADE_WORKLOAD_NOT_READY",
        "Network plugin workloads did not become ready: "
        + ", ".join(f"{kind}/{name}" for kind, _namespace, name in sorted(pending)),
        side_effects=True,
    )


def _from_version(existing: list[dict[str, Any] | None]) -> str | None:
    for document in existing:
        if not isinstance(document, dict):
            continue
        annotations = {k: v for k, v in document.get("metadata", {}).get("annotations", {}).items()}
        version = annotations.get(VERSION_ANNOTATION) if isinstance(annotations, dict) else None
        if isinstance(version, str) and version:
            return version
    return None


def _revision_name(plugin: str) -> str:
    """The mutable pointer that always names the latest captured revision."""

    return f"ai-agent-stack-netrev-{plugin}"


def _history_name(plugin: str, revision: str) -> str:
    """An immutable ConfigMap that holds exactly one captured revision."""

    return f"ai-agent-stack-netrev-{plugin}-{revision.lower()}"


def _snapshot_manifest(
    request: dict[str, Any],
    *,
    name: str,
    revision: str,
    snapshot: dict[str, Any],
    latest_revision: str | None = None,
) -> dict[str, Any]:
    data = {"snapshot": yaml.safe_dump(snapshot, sort_keys=False)}
    if latest_revision is not None:
        data["latest_revision"] = latest_revision
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": name,
            "namespace": request["target_namespace"],
            "labels": {
                MANAGED_BY_LABEL: "target-agent",
                PLUGIN_ANNOTATION: request["plugin"],
            },
            "annotations": {
                PLUGIN_ANNOTATION: request["plugin"],
                VERSION_ANNOTATION: request["target_version"],
                "ai-agent-stack.io/network-revision": revision,
            },
        },
        "data": data,
    }


def _unique_revision(runner, request: dict[str, Any], lease_lost_event) -> str:
    base = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    candidate = base
    suffix = 1
    while _get(
        runner,
        "ConfigMap",
        _history_name(request["plugin"], candidate),
        request["target_namespace"],
        lease_lost_event,
    ) is not None:
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate


def _snapshot_from(document: dict[str, Any], requested: str | None) -> dict[str, Any]:
    data = document.get("data")
    raw = data.get("snapshot") if isinstance(data, dict) else None
    if not isinstance(raw, str):
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_REVISION_INVALID", "The revision ConfigMap has no snapshot."
        )
    try:
        snapshot = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_REVISION_INVALID", "The revision snapshot is invalid YAML."
        ) from exc
    if not isinstance(snapshot, dict):
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_REVISION_INVALID", "The revision snapshot is not an object."
        )
    revision = snapshot.get("revision")
    if not isinstance(revision, str) or not revision:
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_REVISION_INVALID", "The revision snapshot has no revision id."
        )
    if requested and requested != revision:
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_REVISION_MISMATCH",
            f"The stored revision is {revision}, not {requested}.",
        )
    objects = snapshot.get("objects")
    if not isinstance(objects, list) or not objects:
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_REVISION_INVALID", "The revision snapshot has no objects."
        )
    return snapshot


def _read_revision(runner, request: dict[str, Any], lease_lost_event) -> dict[str, Any]:
    requested = request.get("revision")
    if requested:
        history = _get(
            runner,
            "ConfigMap",
            _history_name(request["plugin"], requested),
            request["target_namespace"],
            lease_lost_event,
        )
        if history is not None:
            return _snapshot_from(history, requested)
    document = _get(
        runner,
        "ConfigMap",
        _revision_name(request["plugin"]),
        request["target_namespace"],
        lease_lost_event,
    )
    if document is None:
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_REVISION_NOT_FOUND",
            "No captured revision exists for this network plugin.",
        )
    return _snapshot_from(document, requested)




def _revision_summary(document: dict[str, Any]) -> dict[str, Any] | None:
    data = document.get("data")
    raw = data.get("snapshot") if isinstance(data, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        snapshot = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None
    if not isinstance(snapshot, dict):
        return None
    revision = snapshot.get("revision")
    objects = snapshot.get("objects")
    if not isinstance(revision, str) or not revision or not isinstance(objects, list):
        return None
    metadata = document.get("metadata")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    return {
        "revision": revision,
        "config_map": name,
        "previous_version": snapshot.get("previous_version"),
        "target_version": snapshot.get("target_version"),
        "object_count": len(objects),
    }


def execute_network_revisions(
    payload: dict[str, Any],
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any = None,
    *,
    cluster_name: str | None = None,
) -> dict[str, Any]:
    """List captured revisions for one plugin, newest first.

    The label selector returns immutable history ConfigMaps. The mutable latest
    pointer is read separately so a deployment created before revision history
    still reports the one revision it can roll back to.
    """

    request = validate_revisions_request(payload, cluster_name=cluster_name)
    result = _run(
        runner,
        [
            "get",
            "configmaps",
            "--namespace",
            request["target_namespace"],
            "--selector",
            f"{PLUGIN_ANNOTATION}={request['plugin']}",
            "-o",
            "json",
        ],
        lease_lost_event=lease_lost_event,
        timeout=60,
    )
    try:
        listing = json.loads(result.get("stdout", ""))
    except (TypeError, ValueError) as exc:
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_REVISION_INVALID", "kubectl returned invalid ConfigMap JSON."
        ) from exc
    items = listing.get("items") if isinstance(listing, dict) else None
    if not isinstance(items, list):
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_REVISION_INVALID", "kubectl returned no ConfigMap items."
        )
    rows: dict[str, dict[str, Any]] = {}
    for item in items:
        if isinstance(item, dict):
            summary = _revision_summary(item)
            if summary is not None:
                rows.setdefault(summary["revision"], summary)
    pointer = _get(
        runner,
        "ConfigMap",
        _revision_name(request["plugin"]),
        request["target_namespace"],
        lease_lost_event,
    )
    latest = None
    if pointer is not None:
        data = pointer.get("data")
        if isinstance(data, dict) and isinstance(data.get("latest_revision"), str):
            latest = data["latest_revision"]
        summary = _revision_summary(pointer)
        if summary is not None:
            rows.setdefault(summary["revision"], summary)
    ordered = sorted(rows.values(), key=lambda row: row["revision"], reverse=True)
    return {
        "action": REVISIONS_ACTION,
        "cluster_name": request["target_cluster_name"],
        "namespace": request["target_namespace"],
        "plugin": request["plugin"],
        "latest_revision": latest or (ordered[0]["revision"] if ordered else None),
        "revision_count": len(ordered),
        "revisions": ordered[:20],
        "approval_required": False,
        "side_effects": False,
    }


def execute_network_upgrade(
    payload: dict[str, Any],
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any = None,
    *,
    cluster_name: str | None = None,
) -> dict[str, Any]:
    request = validate_upgrade_request(payload, cluster_name=cluster_name)
    docs = _bundle(
        request["documents"],
        namespace=request["target_namespace"],
        plugin=request["plugin"],
        version=request["target_version"],
    )
    existing = []
    for document in docs:
        existing.append(
            _get(
                runner,
                document["kind"],
                document["metadata"]["name"],
                document["metadata"].get("namespace", ""),
                lease_lost_event,
            )
        )
    previous_version = _from_version(existing)
    objects = [
        {
            "kind": document["kind"],
            "name": document["metadata"]["name"],
            "namespace": document["metadata"].get("namespace"),
            "exists": current is not None,
        }
        for document, current in zip(docs, existing, strict=True)
    ]
    plan = upgrade_plan(request, from_version=previous_version, objects=objects)
    if request["execution_mode"] == "simulation":
        return plan
    crds, others = _split_crds(docs)
    crd_bundle = bool(crds and others)
    if not crd_bundle:
        _apply(runner, docs, lease_lost_event, dry_run=True)
    revision = _unique_revision(runner, request, lease_lost_event)
    snapshot = {
        "revision": revision,
        "plugin": request["plugin"],
        "previous_version": previous_version,
        "target_version": request["target_version"],
        "objects": [
            {
                "kind": document["kind"],
                "name": document["metadata"]["name"],
                "namespace": document["metadata"].get("namespace"),
                "exists": current is not None,
                "document": _sanitize(current) if current is not None else None,
            }
            for document, current in zip(docs, existing, strict=True)
        ],
    }
    history_manifest = _snapshot_manifest(
        request,
        name=_history_name(request["plugin"], revision),
        revision=revision,
        snapshot=snapshot,
    )
    pointer_manifest = _snapshot_manifest(
        request,
        name=_revision_name(request["plugin"]),
        revision=revision,
        snapshot=snapshot,
        latest_revision=revision,
    )
    _apply(runner, [history_manifest], lease_lost_event, dry_run=False)
    if crd_bundle:
        # Apply the approved CRD first; only then can the API server validate a
        # custom resource that arrives in the same bundle.
        _apply(runner, crds, lease_lost_event, dry_run=False)
        try:
            _apply(runner, others, lease_lost_event, dry_run=True)
        except NetworkUpgradeExecutionError as exc:
            raise NetworkUpgradeExecutionError(
                exc.code,
                "The approved CRDs were applied before the remaining objects failed validation: "
                + exc.detail,
                side_effects=True,
            ) from exc
    try:
        _apply(runner, [pointer_manifest], lease_lost_event, dry_run=False)
        _apply(runner, others if crd_bundle else docs, lease_lost_event, dry_run=False)
    except NetworkUpgradeExecutionError as exc:
        if not crd_bundle:
            raise
        raise NetworkUpgradeExecutionError(
            exc.code,
            "The approved CRDs and revision were written before the bundle failed: "
            + exc.detail,
            side_effects=True,
        ) from exc
    _wait_workloads(runner, docs, lease_lost_event)
    return {
        **plan,
        "execution_mode": "execute",
        "upgrade_simulated": False,
        "upgrade_performed": True,
        "side_effects": True,
        "revision": revision,
        "revision_config_map": _history_name(request["plugin"], revision),
        "crds_applied_first": crd_bundle,
        "ready_workloads": [
            {"kind": document["kind"], "name": document["metadata"]["name"]}
            for document in docs
            if document["kind"] in WORKLOAD_KINDS
        ],
    }


def execute_network_rollback(
    payload: dict[str, Any],
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any = None,
    *,
    cluster_name: str | None = None,
) -> dict[str, Any]:
    request = validate_rollback_request(payload, cluster_name=cluster_name)
    snapshot = _read_revision(runner, request, lease_lost_event)
    previous_version = snapshot.get("previous_version")
    if previous_version is not None and not isinstance(previous_version, str):
        raise NetworkUpgradeExecutionError(
            "NETWORK_UPGRADE_REVISION_INVALID", "previous_version is malformed."
        )
    plan = rollback_plan(request, revision=snapshot["revision"], previous_version=previous_version)
    if request["execution_mode"] == "simulation":
        return plan
    restore: list[dict[str, Any]] = []
    remove: list[tuple[str, str, str]] = []
    for entry in snapshot["objects"]:
        if not isinstance(entry, dict):
            raise NetworkUpgradeExecutionError(
                "NETWORK_UPGRADE_REVISION_INVALID", "A revision object is malformed."
            )
        kind = entry.get("kind")
        name = entry.get("name")
        namespace = entry.get("namespace") or ""
        if not isinstance(kind, str) or not isinstance(name, str):
            raise NetworkUpgradeExecutionError(
                "NETWORK_UPGRADE_REVISION_INVALID", "A revision identity is malformed."
            )
        document = entry.get("document")
        if entry.get("exists") is True:
            if not isinstance(document, dict):
                raise NetworkUpgradeExecutionError(
                    "NETWORK_UPGRADE_REVISION_INVALID", "A captured document is missing."
                )
            restore.append(document)
        else:
            remove.append((kind, name, namespace))
    restore_crds, restore_others = _split_crds(restore)
    crd_restore = bool(restore_crds and restore_others)
    if crd_restore:
        _apply(runner, restore_crds, lease_lost_event, dry_run=False)
        try:
            _apply(runner, restore_others, lease_lost_event, dry_run=True)
        except NetworkUpgradeExecutionError as exc:
            raise NetworkUpgradeExecutionError(
                exc.code,
                "The captured CRDs were restored before the remaining objects failed validation: "
                + exc.detail,
                side_effects=True,
            ) from exc
    elif restore:
        _apply(runner, restore, lease_lost_event, dry_run=True)
    # A CRD owns its custom resources, so remove resources before definitions.
    remove.sort(key=lambda item: item[0] == "CustomResourceDefinition")
    for kind, name, namespace in remove:
        arguments = ["delete", kind, name]
        if namespace:
            arguments += ["--namespace", namespace]
        arguments += ["--ignore-not-found=true", "--dry-run=server"]
        _run(runner, arguments, lease_lost_event=lease_lost_event, timeout=120)
    if restore:
        _apply(
            runner,
            restore_others if crd_restore else restore,
            lease_lost_event,
            dry_run=False,
        )
    for kind, name, namespace in remove:
        _delete(runner, kind, name, namespace, lease_lost_event)
    if restore:
        _wait_workloads(runner, restore, lease_lost_event)
    return {
        **plan,
        "execution_mode": "execute",
        "rollback_simulated": False,
        "rollback_performed": True,
        "side_effects": True,
        "restored_objects": [
            {"kind": document["kind"], "name": document["metadata"]["name"]} for document in restore
        ],
        "removed_objects": [
            {"kind": kind, "name": name, "namespace": namespace or None}
            for kind, name, namespace in remove
        ],
    }


__all__ = [
    "NetworkUpgradeExecutionError",
    "REVISIONS_ACTION",
    "UPGRADE_ACTION",
    "execute_network_revisions",
    "execute_network_rollback",
    "execute_network_upgrade",
]
