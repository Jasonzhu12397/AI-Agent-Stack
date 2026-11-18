from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

CLEANUP_LABEL = "ai-agent-stack.io/eph-cleanup"
CLEANUP_ANNOTATION = "ai-agent-stack.io/eph-cleanup"
DEPLOYMENT_ID_LABEL = "ai-agent-stack.io/deployment-id"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
SOURCE_UID_ANNOTATION = "ai-agent-stack.io/source-cluster-uid"
CREDENTIAL_SCOPE_ANNOTATION = "ai-agent-stack.io/credential-scope"
TARGET_OBSERVER_SECRET = "target-agent-source-operator"
NAMESPACE = "ai-agent-system"
RESOURCE_SCOPES = (
    ("deployments.apps", True),
    ("statefulsets.apps", True),
    ("daemonsets.apps", True),
    ("jobs.batch", True),
    ("pods", True),
    ("configmaps", True),
    ("secrets", True),
    ("clusterrolebindings.rbac.authorization.k8s.io", False),
    ("clusterroles.rbac.authorization.k8s.io", False),
    ("roles.rbac.authorization.k8s.io", True),
    ("rolebindings.rbac.authorization.k8s.io", True),
    ("serviceaccounts", True),
)


class CleanupScopeError(ValueError):
    def __init__(self, message: str, side_effects: bool = False) -> None:
        super().__init__(message)
        self.side_effects = side_effects


def _run(
    runner: Callable[..., dict[str, Any]],
    kubeconfig: str,
    arguments: list[str],
    lease_lost_event: Any = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    if lease_lost_event is not None and lease_lost_event.is_set():
        raise CleanupScopeError("EPH_CLEANUP_SCOPE_COMMAND_LEASE_LOST")
    result = runner(
        ["kubectl", "--kubeconfig", kubeconfig, *arguments],
        kubeconfig,
        timeout_seconds=timeout_seconds,
        lease_lost_event=lease_lost_event,
        max_output_bytes=8 * 1024 * 1024,
    )
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise CleanupScopeError("EPH_CLEANUP_SCOPE_KUBERNETES_API_FAILED")
    return result


def _namespace_args(namespaced: bool, namespace: str = NAMESPACE) -> list[str]:
    return ["--namespace", namespace] if namespaced else []


def _get(
    runner: Callable[..., dict[str, Any]],
    kubeconfig: str,
    resource: str,
    name: str,
    namespaced: bool,
    lease_lost_event: Any = None,
    namespace: str = NAMESPACE,
) -> dict[str, Any]:
    result = _run(
        runner,
        kubeconfig,
        ["get", resource, name, *_namespace_args(namespaced, namespace), "-o", "json"],
        lease_lost_event,
    )
    try:
        document = json.loads(result.get("stdout", ""))
    except (TypeError, ValueError) as exc:
        raise CleanupScopeError("EPH_CLEANUP_SCOPE_RESOURCE_JSON_INVALID") from exc
    if not isinstance(document, dict):
        raise CleanupScopeError("EPH_CLEANUP_SCOPE_RESOURCE_JSON_INVALID")
    return document


def _patch_deployment_template_source_uid(
    runner: Callable[..., dict[str, Any]],
    kubeconfig: str,
    deployment: dict[str, Any],
    deployment_id: str,
    source_cluster_uid: str,
    lease_lost_event: Any = None,
) -> bool:
    metadata = deployment.get("metadata", {})
    template_metadata = (
        deployment.get("spec", {})
        .get("template", {})
        .get("metadata", {})
    )
    labels = template_metadata.get("labels", {})
    annotations = template_metadata.get("annotations", {})
    if not isinstance(labels, dict) or not isinstance(annotations, dict):
        raise CleanupScopeError("EPH_CLEANUP_SCOPE_DEPLOYMENT_TEMPLATE_METADATA_INVALID")
    if (
        labels.get(MANAGED_BY_LABEL) != "ai-agent-stack"
        or labels.get(CLEANUP_LABEL) != "true"
        or labels.get(DEPLOYMENT_ID_LABEL) != deployment_id
        or annotations.get(CLEANUP_ANNOTATION) != "true"
    ):
        raise CleanupScopeError("EPH_CLEANUP_SCOPE_DEPLOYMENT_TEMPLATE_OWNERSHIP_MISMATCH")
    current_source_uid = annotations.get(SOURCE_UID_ANNOTATION)
    if current_source_uid not in (None, source_cluster_uid):
        raise CleanupScopeError("EPH_CLEANUP_SCOPE_DEPLOYMENT_TEMPLATE_SOURCE_UID_MISMATCH")
    if current_source_uid == source_cluster_uid:
        return False
    uid = metadata.get("uid")
    if not isinstance(uid, str) or not uid:
        raise CleanupScopeError("EPH_CLEANUP_SCOPE_DEPLOYMENT_UID_MISSING")
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": uid},
        {
            "op": "add",
            "path": (
                "/spec/template/metadata/annotations/"
                "ai-agent-stack.io~1source-cluster-uid"
            ),
            "value": source_cluster_uid,
        },
    ]
    try:
        _run(
            runner,
            kubeconfig,
            [
                "patch",
                "deployments.apps",
                metadata["name"],
                "--namespace",
                NAMESPACE,
                "--type=json",
                "-p",
                json.dumps(patch, separators=(",", ":")),
            ],
            lease_lost_event,
        )
    except CleanupScopeError as exc:
        exc.side_effects = True
        raise
    verified = _get(
        runner,
        kubeconfig,
        "deployments.apps",
        metadata["name"],
        True,
        lease_lost_event,
    )
    verified_annotations = (
        verified.get("spec", {})
        .get("template", {})
        .get("metadata", {})
        .get("annotations", {})
    )
    if verified_annotations.get(SOURCE_UID_ANNOTATION) != source_cluster_uid:
        raise CleanupScopeError(
            "EPH_CLEANUP_SCOPE_DEPLOYMENT_TEMPLATE_SOURCE_UID_VERIFICATION_FAILED",
            side_effects=True,
        )
    return True


def _patch_source_uid(
    runner: Callable[..., dict[str, Any]],
    kubeconfig: str,
    resource: str,
    name: str,
    namespaced: bool,
    document: dict[str, Any],
    deployment_id: str,
    source_cluster_uid: str,
    lease_lost_event: Any = None,
    namespace: str = NAMESPACE,
) -> bool:
    metadata = document.get("metadata", {})
    labels = metadata.get("labels", {})
    annotations = metadata.get("annotations", {})
    if not isinstance(metadata, dict) or not isinstance(labels, dict) or not isinstance(
        annotations,
        dict,
    ):
        raise CleanupScopeError("EPH_CLEANUP_SCOPE_RESOURCE_METADATA_INVALID")
    if (
        labels.get(MANAGED_BY_LABEL) != "ai-agent-stack"
        or labels.get(CLEANUP_LABEL) != "true"
        or labels.get(DEPLOYMENT_ID_LABEL) != deployment_id
        or annotations.get(CLEANUP_ANNOTATION) != "true"
    ):
        raise CleanupScopeError("EPH_CLEANUP_SCOPE_RESOURCE_OWNERSHIP_MISMATCH")
    current_source_uid = annotations.get(SOURCE_UID_ANNOTATION)
    if current_source_uid not in (None, source_cluster_uid):
        raise CleanupScopeError("EPH_CLEANUP_SCOPE_SOURCE_UID_MISMATCH")
    uid = metadata.get("uid")
    if not isinstance(uid, str) or not uid:
        raise CleanupScopeError("EPH_CLEANUP_SCOPE_RESOURCE_UID_MISSING")
    if current_source_uid == source_cluster_uid:
        if resource == "deployments.apps" and name == "eph-agent":
            return _patch_deployment_template_source_uid(
                runner,
                kubeconfig,
                document,
                deployment_id,
                source_cluster_uid,
                lease_lost_event,
            )
        return False
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": uid},
        {
            "op": "add",
            "path": "/metadata/annotations/ai-agent-stack.io~1source-cluster-uid",
            "value": source_cluster_uid,
        },
    ]
    try:
        _run(
            runner,
            kubeconfig,
            [
                "patch",
                resource,
                name,
                *_namespace_args(namespaced, namespace),
                "--type=json",
                "-p",
                json.dumps(patch, separators=(",", ":")),
            ],
            lease_lost_event,
        )
    except CleanupScopeError as exc:
        exc.side_effects = True
        raise
    verified = _get(
        runner,
        kubeconfig,
        resource,
        name,
        namespaced,
        lease_lost_event,
        namespace,
    )
    verified_annotations = verified.get("metadata", {}).get("annotations", {})
    if verified_annotations.get(SOURCE_UID_ANNOTATION) != source_cluster_uid:
        raise CleanupScopeError(
            "EPH_CLEANUP_SCOPE_SOURCE_UID_VERIFICATION_FAILED",
            side_effects=True,
        )
    if resource == "deployments.apps" and name == "eph-agent":
        _patch_deployment_template_source_uid(
            runner,
            kubeconfig,
            verified,
            deployment_id,
            source_cluster_uid,
            lease_lost_event,
        )
    return True


def attest_cleanup_scope(
    source_kubeconfig: str,
    target_kubeconfig: str,
    deployment_id: str,
    source_cluster_uid: str,
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any = None,
) -> dict[str, Any]:
    if not isinstance(deployment_id, str) or not deployment_id:
        raise CleanupScopeError("EPH_CLEANUP_SCOPE_DEPLOYMENT_ID_REQUIRED")
    if not isinstance(source_cluster_uid, str) or not source_cluster_uid:
        raise CleanupScopeError("EPH_CLEANUP_SCOPE_SOURCE_UID_REQUIRED")
    mutated = 0
    try:
        optional_target_kubeconfig_marked = False
        selector = (
            f"{MANAGED_BY_LABEL}=ai-agent-stack,"
            f"{CLEANUP_LABEL}=true,"
            f"{DEPLOYMENT_ID_LABEL}={deployment_id}"
        )
        observed = {}
        for resource, namespaced in RESOURCE_SCOPES:
            result = _run(
                runner,
                source_kubeconfig,
                [
                    "get",
                    resource,
                    *_namespace_args(namespaced),
                    "--selector",
                    selector,
                    "-o",
                    "json",
                ],
                lease_lost_event,
                timeout_seconds=60,
            )
            try:
                document = json.loads(result.get("stdout", ""))
            except (TypeError, ValueError) as exc:
                raise CleanupScopeError(
                    "EPH_CLEANUP_SCOPE_INVENTORY_JSON_INVALID",
                    side_effects=mutated > 0,
                ) from exc
            items = document.get("items")
            if not isinstance(items, list):
                raise CleanupScopeError(
                    "EPH_CLEANUP_SCOPE_INVENTORY_INVALID",
                    side_effects=mutated > 0,
                )
            observed[resource] = items
        required = {
            "deployments.apps": {"eph-agent"},
            "serviceaccounts": {"eph-agent"},
            "clusterroles.rbac.authorization.k8s.io": {"eph-agent-preflight"},
            "clusterrolebindings.rbac.authorization.k8s.io": {"eph-agent-preflight"},
            "secrets": {
                "eph-agent-credentials",
                "agent-control-plane-mtls",
                "capi-pivot-source-kubeconfig",
            },
        }
        for resource, expected_names in required.items():
            actual_names = {
                item.get("metadata", {}).get("name")
                for item in observed.get(resource, [])
                if isinstance(item, dict)
            }
            if not expected_names.issubset(actual_names):
                raise CleanupScopeError(
                    f"EPH_CLEANUP_SCOPE_REQUIRED_RESOURCE_MISSING:{resource}",
                    side_effects=mutated > 0,
                )
        source_resource_count = 0
        for resource, namespaced in RESOURCE_SCOPES:
            for item in observed[resource]:
                metadata = item.get("metadata", {})
                name = metadata.get("name")
                if not isinstance(name, str) or not name:
                    raise CleanupScopeError(
                        "EPH_CLEANUP_SCOPE_RESOURCE_NAME_MISSING",
                        side_effects=mutated > 0,
                    )
                source_resource_count += 1
                if _patch_source_uid(
                    runner,
                    source_kubeconfig,
                    resource,
                    name,
                    namespaced,
                    item,
                    deployment_id,
                    source_cluster_uid,
                    lease_lost_event,
                ):
                    mutated += 1
        for resource in (
            "roles.rbac.authorization.k8s.io",
            "rolebindings.rbac.authorization.k8s.io",
        ):
            result = _run(
                runner,
                source_kubeconfig,
                [
                    "get",
                    resource,
                    "--namespace",
                    "metal3",
                    "--selector",
                    selector,
                    "-o",
                    "json",
                ],
                lease_lost_event,
                timeout_seconds=60,
            )
            try:
                document = json.loads(result.get("stdout", ""))
            except (TypeError, ValueError) as exc:
                raise CleanupScopeError(
                    "EPH_CLEANUP_SCOPE_METAL3_RBAC_INVENTORY_JSON_INVALID",
                    side_effects=mutated > 0,
                ) from exc
            items = document.get("items")
            if not isinstance(items, list):
                raise CleanupScopeError(
                    "EPH_CLEANUP_SCOPE_METAL3_RBAC_INVENTORY_INVALID",
                    side_effects=mutated > 0,
                )
            for item in items:
                metadata = item.get("metadata", {})
                name = metadata.get("name")
                if not isinstance(name, str) or not name:
                    raise CleanupScopeError(
                        "EPH_CLEANUP_SCOPE_METAL3_RBAC_RESOURCE_NAME_MISSING",
                        side_effects=mutated > 0,
                    )
                source_resource_count += 1
                if _patch_source_uid(
                    runner,
                    source_kubeconfig,
                    resource,
                    name,
                    True,
                    item,
                    deployment_id,
                    source_cluster_uid,
                    lease_lost_event,
                    "metal3",
                ):
                    mutated += 1
        if source_resource_count == 0:
            raise CleanupScopeError(
                "EPH_CLEANUP_SCOPE_SOURCE_RESOURCES_EMPTY",
                side_effects=mutated > 0,
            )
        target_secret = _get(
            runner,
            target_kubeconfig,
            "secret",
            TARGET_OBSERVER_SECRET,
            True,
            lease_lost_event,
        )
        target_metadata = target_secret.get("metadata", {})
        target_data = target_secret.get("data", {})
        if (
            target_metadata.get("name") != TARGET_OBSERVER_SECRET
            or target_metadata.get("namespace") != NAMESPACE
            or not isinstance(target_data, dict)
        ):
            raise CleanupScopeError(
                "EPH_CLEANUP_SCOPE_TARGET_OBSERVER_SECRET_IDENTITY_MISMATCH",
                side_effects=mutated > 0,
            )
        encoded_source_kubeconfig = target_data.get("kubeconfig")
        if not isinstance(encoded_source_kubeconfig, str) or not encoded_source_kubeconfig:
            raise CleanupScopeError(
                "EPH_CLEANUP_SCOPE_TARGET_OBSERVER_SECRET_DATA_MISSING",
                side_effects=mutated > 0,
            )
        if not Path(source_kubeconfig).is_file():
            raise CleanupScopeError(
                "EPH_CLEANUP_SCOPE_SOURCE_KUBECONFIG_FILE_UNAVAILABLE",
                side_effects=mutated > 0,
            )
        try:
            secret_source_kubeconfig = base64.b64decode(
                encoded_source_kubeconfig,
                validate=True,
            )
            mounted_source_kubeconfig = Path(source_kubeconfig).read_bytes()
        except (OSError, ValueError) as exc:
            raise CleanupScopeError(
                "EPH_CLEANUP_SCOPE_TARGET_OBSERVER_SECRET_DATA_INVALID",
                side_effects=mutated > 0,
            ) from exc
        if (
            not secret_source_kubeconfig
            or secret_source_kubeconfig != mounted_source_kubeconfig
        ):
            raise CleanupScopeError(
                "EPH_CLEANUP_SCOPE_TARGET_OBSERVER_SECRET_IDENTITY_MISMATCH",
                side_effects=mutated > 0,
            )
        target_mutated = _patch_source_uid(
            runner,
            target_kubeconfig,
            "secret",
            TARGET_OBSERVER_SECRET,
            True,
            target_secret,
            deployment_id,
            source_cluster_uid,
            lease_lost_event,
        )
        mutated += int(target_mutated)
    except CleanupScopeError as exc:
        exc.side_effects = exc.side_effects or mutated > 0
        raise
    except Exception as exc:
        if mutated > 0:
            raise CleanupScopeError(
                "EPH_CLEANUP_SCOPE_EXECUTION_FAILED",
                side_effects=True,
            ) from exc
        raise
    return {
        "cleanup_scope_attested": True,
        "source_cleanup_resource_count": source_resource_count,
        "target_observer_secret_source_uid_attested": True,
        "optional_target_kubeconfig_marked": optional_target_kubeconfig_marked,
        "mutated_resource_count": mutated,
    }
