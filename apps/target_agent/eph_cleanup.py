from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from typing import Any

CLEANUP_ANNOTATION = "ai-agent-stack.io/eph-cleanup"
SOURCE_UID_ANNOTATION = "ai-agent-stack.io/source-cluster-uid"
DEPLOYMENT_ID_ANNOTATION = "ai-agent-stack.io/deployment-id"
CREDENTIAL_SCOPE_ANNOTATION = "ai-agent-stack.io/credential-scope"

RESOURCE_ORDER = (
    "deployments.apps",
    "statefulsets.apps",
    "daemonsets.apps",
    "jobs.batch",
    "pods",
    "configmaps",
    "secrets",
)
MAX_INVENTORY_BYTES = 8 * 1024 * 1024


class EphCleanupError(ValueError):
    pass


def _valid_name(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 63
        or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", value)
    ):
        raise EphCleanupError(f"EPH_CLEANUP_{field}_INVALID")
    return value


def _attestation(context: Any) -> dict[str, Any]:
    if not isinstance(context, dict):
        raise EphCleanupError("EPH_CLEANUP_TRUSTED_PIVOT_CONTEXT_REQUIRED")
    proof = context.get("eph_pivot_attestation")
    if not isinstance(proof, dict):
        raise EphCleanupError("EPH_CLEANUP_TRUSTED_PIVOT_ATTESTATION_REQUIRED")
    required = (
        "source_cluster_uid",
        "target_cluster_uid",
        "source_bmh_digest",
        "target_bmh_digest",
        "source_agent_id",
        "command_id",
        "attested_at",
    )
    if not all(isinstance(proof.get(key), str) and proof[key] for key in required):
        raise EphCleanupError("EPH_CLEANUP_TRUSTED_PIVOT_ATTESTATION_INVALID")
    if not re.fullmatch(r"[a-f0-9]{64}", proof["source_bmh_digest"]):
        raise EphCleanupError("EPH_CLEANUP_SOURCE_BMH_DIGEST_INVALID")
    if proof["source_bmh_digest"] != proof["target_bmh_digest"]:
        raise EphCleanupError("EPH_CLEANUP_BMH_INVENTORY_NOT_PRESERVED")
    if proof["source_cluster_uid"] == proof["target_cluster_uid"]:
        raise EphCleanupError("EPH_CLEANUP_CLUSTER_UIDS_NOT_DISTINCT")
    return proof


def _run(
    runner: Callable[..., dict[str, Any]],
    arguments: list[str],
    kubeconfig: str,
    timeout: int = 60,
) -> dict[str, Any]:
    result = runner(
        arguments,
        timeout=timeout,
        max_output_bytes=MAX_INVENTORY_BYTES,
        kubeconfig=kubeconfig,
    )
    if not isinstance(result, dict) or result.get("available") is not True:
        raise EphCleanupError("EPH_CLEANUP_SOURCE_API_OPERATION_FAILED")
    return result


def execute_eph_cleanup(
    payload: dict[str, Any],
    deployment_context: dict[str, Any] | None,
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any = None,
    environ: Any = None,
) -> dict[str, Any]:
    values = os.environ if environ is None else environ
    proof = _attestation(deployment_context)
    source_kubeconfig = values.get("CAPI_PIVOT_SOURCE_KUBECONFIG", "")
    if not isinstance(source_kubeconfig, str) or not source_kubeconfig:
        raise EphCleanupError("EPH_CLEANUP_SOURCE_KUBECONFIG_REQUIRED")
    if not os.path.isfile(source_kubeconfig):
        raise EphCleanupError("EPH_CLEANUP_SOURCE_KUBECONFIG_UNAVAILABLE")

    namespace = _valid_name(payload.get("namespace"), "NAMESPACE")
    deployment_id = _valid_name(payload.get("deployment_id"), "DEPLOYMENT_ID")
    if payload.get("confirm_source_cluster_uid") != proof["source_cluster_uid"]:
        raise EphCleanupError("EPH_CLEANUP_SOURCE_CLUSTER_CONFIRMATION_MISMATCH")
    if payload.get("confirm_target_cluster_uid") != proof["target_cluster_uid"]:
        raise EphCleanupError("EPH_CLEANUP_TARGET_CLUSTER_CONFIRMATION_MISMATCH")

    selector = (
        f"{CLEANUP_ANNOTATION}=true,"
        f"app.kubernetes.io/managed-by=ai-agent-stack,"
        f"ai-agent-stack.io/deployment-id={deployment_id}"
    )
    inventory: list[dict[str, Any]] = []
    for resource in RESOURCE_ORDER:
        if lease_lost_event is not None and lease_lost_event.is_set():
            raise EphCleanupError("EPH_CLEANUP_COMMAND_LEASE_LOST")
        result = _run(
            runner,
            [
                "get",
                resource,
                "--namespace",
                namespace,
                "--selector",
                selector,
                "-o",
                "json",
            ],
            source_kubeconfig,
        )
        try:
            document = json.loads(result.get("stdout", ""))
        except (TypeError, ValueError) as exc:
            raise EphCleanupError("EPH_CLEANUP_INVENTORY_JSON_INVALID") from exc
        items = document.get("items") if isinstance(document, dict) else None
        if not isinstance(items, list):
            raise EphCleanupError("EPH_CLEANUP_INVENTORY_SHAPE_INVALID")
        for item in items:
            metadata = item.get("metadata", {}) if isinstance(item, dict) else {}
            annotations = metadata.get("annotations", {})
            labels = metadata.get("labels", {})
            name = metadata.get("name")
            item_namespace = metadata.get("namespace")
            if (
                not isinstance(annotations, dict)
                or not isinstance(labels, dict)
                or annotations.get(CLEANUP_ANNOTATION) != "true"
                or labels.get(CLEANUP_ANNOTATION) != "true"
                or annotations.get(SOURCE_UID_ANNOTATION) != proof["source_cluster_uid"]
                or labels.get(DEPLOYMENT_ID_ANNOTATION) != deployment_id
                or labels.get("app.kubernetes.io/managed-by") != "ai-agent-stack"
                or item_namespace != namespace
                or not isinstance(name, str)
                or not isinstance(metadata.get("uid"), str)
                or not metadata.get("uid")
            ):
                raise EphCleanupError("EPH_CLEANUP_RESOURCE_OWNERSHIP_MISMATCH")
            inventory.append({
                "resource": resource,
                "name": _valid_name(name, "RESOURCE_NAME"),
                "namespace": namespace,
                "uid": metadata.get("uid"),
                "credential": (
                    resource == "secrets"
                    and annotations.get(CREDENTIAL_SCOPE_ANNOTATION) == "eph-agent"
                ),
            })

    workload_resources = {
        "deployments.apps",
        "statefulsets.apps",
        "daemonsets.apps",
        "jobs.batch",
        "pods",
    }
    if not any(item["resource"] in workload_resources for item in inventory):
        raise EphCleanupError("EPH_CLEANUP_MANAGED_WORKLOAD_INVENTORY_EMPTY")
    if not any(item["credential"] for item in inventory):
        raise EphCleanupError("EPH_CLEANUP_EPH_CREDENTIAL_INVENTORY_EMPTY")

    deleted: list[dict[str, Any]] = []
    for item in inventory:
        if lease_lost_event is not None and lease_lost_event.is_set():
            raise EphCleanupError("EPH_CLEANUP_COMMAND_LEASE_LOST")
        arguments = [
            "delete",
            item["resource"],
            item["name"],
            "--namespace",
            namespace,
            "--wait=true",
            "--timeout=120s",
            "--ignore-not-found=true",
        ]
        current_result = runner(
            [
                "get",
                item["resource"],
                item["name"],
                "--namespace",
                namespace,
                "-o",
                "json",
            ],
            timeout=30,
            max_output_bytes=MAX_INVENTORY_BYTES,
            kubeconfig=source_kubeconfig,
        )
        if current_result.get("available") is True:
            try:
                current = json.loads(current_result.get("stdout", ""))
            except (TypeError, ValueError) as exc:
                raise EphCleanupError("EPH_CLEANUP_RESOURCE_IDENTITY_INVALID") from exc
            current_metadata = current.get("metadata", {})
            if (
                current_metadata.get("uid") != item["uid"]
                or current_metadata.get("annotations", {}).get(SOURCE_UID_ANNOTATION)
                != proof["source_cluster_uid"]
            ):
                raise EphCleanupError("EPH_CLEANUP_RESOURCE_IDENTITY_CHANGED")
        elif "notfound" not in current_result.get("stderr", "").lower() and (
            "not found" not in current_result.get("stderr", "").lower()
        ):
            raise EphCleanupError("EPH_CLEANUP_RESOURCE_PREDELETE_OBSERVATION_FAILED")
        _run(runner, arguments, source_kubeconfig, timeout=150)
        verify = runner(
            [
                "get",
                item["resource"],
                item["name"],
                "--namespace",
                namespace,
                "-o",
                "json",
            ],
            timeout=30,
            max_output_bytes=MAX_INVENTORY_BYTES,
            kubeconfig=source_kubeconfig,
        )
        if verify.get("available") is True:
            try:
                current = json.loads(verify.get("stdout", ""))
            except (TypeError, ValueError) as exc:
                raise EphCleanupError("EPH_CLEANUP_RESOURCE_VERIFY_JSON_INVALID") from exc
            if current.get("metadata", {}).get("uid") == item["uid"]:
                raise EphCleanupError("EPH_CLEANUP_RESOURCE_STILL_PRESENT")
        elif "notfound" not in verify.get("stderr", "").lower() and (
            "not found" not in verify.get("stderr", "").lower()
        ):
            raise EphCleanupError("EPH_CLEANUP_RESOURCE_VERIFY_FAILED")
        deleted.append(item)

    remaining = 0
    for resource in RESOURCE_ORDER:
        if lease_lost_event is not None and lease_lost_event.is_set():
            raise EphCleanupError("EPH_CLEANUP_COMMAND_LEASE_LOST")
        result = _run(
            runner,
            [
                "get",
                resource,
                "--namespace",
                namespace,
                "--selector",
                selector,
                "-o",
                "json",
            ],
            source_kubeconfig,
        )
        try:
            document = json.loads(result.get("stdout", ""))
            items = document.get("items")
        except (TypeError, ValueError) as exc:
            raise EphCleanupError("EPH_CLEANUP_FINAL_INVENTORY_INVALID") from exc
        if not isinstance(items, list):
            raise EphCleanupError("EPH_CLEANUP_FINAL_INVENTORY_INVALID")
        remaining += len(items)
    if remaining:
        raise EphCleanupError("EPH_CLEANUP_RESOURCES_REMAIN")

    return {
        "eph_cleanup_verified": True,
        "eph_workloads_absent": True,
        "eph_credentials_revoked": True,
        "eph_api_no_longer_required": True,
        "target_agent_owns_handoff": True,
        "source_cluster_uid": proof["source_cluster_uid"],
        "target_cluster_uid": proof["target_cluster_uid"],
        "cleanup_namespace": namespace,
        "cleanup_resource_count": len(inventory),
        "deleted_resource_count": len(deleted),
        "revoked_credential_count": sum(item["credential"] for item in deleted),
        "cleanup_resource_inventory": deleted,
        "side_effects": bool(deleted),
    }
