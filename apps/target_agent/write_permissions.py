"""Fail-closed Kubernetes permission preflight for Target Agent write actions.

The Target Agent mutates the Target Cluster through ``kubectl``. A missing RBAC
rule used to surface halfway through an apply sequence, after some resources had
already been written. This module asks the API server the same question the
apply will ask (``kubectl auth can-i``) before the first write, using a
declarative requirement table per action. It never infers permissions from the
payload and never treats an API transport failure as an allow.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class WritePermissionError(RuntimeError):
    """One or more required write permissions are absent or could not be checked."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Permission:
    """A single ``kubectl auth can-i`` question."""

    verb: str
    resource: str
    namespace_scoped: bool = True


#: Resources the Worker provisioning executor server-side-applies.
WORKER_PROVISION_PERMISSIONS: tuple[Permission, ...] = (
    Permission("get", "customresourcedefinitions.apiextensions.k8s.io", False),
    Permission("create", "machinedeployments.cluster.x-k8s.io"),
    Permission("patch", "machinedeployments.cluster.x-k8s.io"),
    Permission("create", "kubeadmconfigtemplates.bootstrap.cluster.x-k8s.io"),
    Permission("patch", "kubeadmconfigtemplates.bootstrap.cluster.x-k8s.io"),
    Permission("create", "metal3machinetemplates.infrastructure.cluster.x-k8s.io"),
    Permission("patch", "metal3machinetemplates.infrastructure.cluster.x-k8s.io"),
    Permission("create", "metal3datatemplates.infrastructure.cluster.x-k8s.io"),
    Permission("patch", "metal3datatemplates.infrastructure.cluster.x-k8s.io"),
    Permission("get", "baremetalhosts.metal3.io"),
    Permission("list", "baremetalhosts.metal3.io"),
    Permission("create", "configmaps"),
    Permission("patch", "configmaps"),
)

#: Resources the network / storage / SR-IOV apply path may write. The operator
#: bundles are vendored by the platform; this is the exact surface they use.
NETWORK_APPLY_PERMISSIONS: tuple[Permission, ...] = (
    Permission("get", "customresourcedefinitions.apiextensions.k8s.io", False),
    Permission("create", "network-attachment-definitions.k8s.cni.cncf.io"),
    Permission("patch", "network-attachment-definitions.k8s.cni.cncf.io"),
    Permission("create", "deployments.apps"),
    Permission("patch", "deployments.apps"),
    Permission("create", "daemonsets.apps"),
    Permission("patch", "daemonsets.apps"),
    Permission("create", "statefulsets.apps"),
    Permission("patch", "statefulsets.apps"),
    Permission("create", "services"),
    Permission("patch", "services"),
    Permission("create", "configmaps"),
    Permission("patch", "configmaps"),
    Permission("create", "storageclasses.storage.k8s.io", False),
    Permission("patch", "storageclasses.storage.k8s.io", False),
    Permission("create", "sriovnetworknodepolicies.sriovnetwork.openshift.io"),
    Permission("patch", "sriovnetworknodepolicies.sriovnetwork.openshift.io"),
    Permission("create", "cephclusters.ceph.rook.io"),
    Permission("patch", "cephclusters.ceph.rook.io"),
    Permission("create", "cephblockpools.ceph.rook.io"),
    Permission("patch", "cephblockpools.ceph.rook.io"),
    Permission("create", "cephfilesystems.ceph.rook.io"),
    Permission("patch", "cephfilesystems.ceph.rook.io"),
)


def _namespace(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise WritePermissionError("TARGET_WRITE_NAMESPACE_REQUIRED")
    return value


def _allowed(
    runner: Callable[..., dict[str, Any]],
    permission: Permission,
    namespace: str | None,
    lease_lost_event: Any,
) -> bool:
    arguments = ["auth", "can-i", permission.verb, permission.resource]
    if permission.namespace_scoped and namespace is not None:
        arguments.extend(["--namespace", namespace])
    result = runner(arguments, timeout=15, lease_lost_event=lease_lost_event)
    if result.get("available") is not True:
        raise WritePermissionError("TARGET_WRITE_PERMISSION_CHECK_UNAVAILABLE")
    if result.get("returncode") != 0:
        raise WritePermissionError("TARGET_WRITE_PERMISSION_CHECK_FAILED")
    answer = result.get("stdout", "").strip().lower()
    if answer == "yes":
        return True
    if answer == "no":
        return False
    raise WritePermissionError("TARGET_WRITE_PERMISSION_ANSWER_INVALID")


def verify_write_permissions(
    action: str,
    *,
    runner: Callable[..., dict[str, Any]],
    namespace: str | None = None,
    lease_lost_event: Any = None,
    environ: Any = None,
) -> None:
    """Verify every permission an action needs before it writes its first byte."""

    values = os.environ if environ is None else environ
    if action == "target.worker.provision":
        required = WORKER_PROVISION_PERMISSIONS
        target_namespace = _namespace(namespace or values.get("TARGET_CAPI_NAMESPACE"))
    elif action == "target.network.apply":
        required = NETWORK_APPLY_PERMISSIONS
        target_namespace = _namespace(namespace or values.get("TARGET_CAPI_NAMESPACE"))
    else:
        raise WritePermissionError("TARGET_WRITE_PERMISSION_ACTION_UNKNOWN")

    for permission in required:
        scope = target_namespace if permission.namespace_scoped else None
        if not _allowed(runner, permission, scope, lease_lost_event):
            raise WritePermissionError(
                "TARGET_RBAC_INSUFFICIENT:"
                f"{permission.verb}:{permission.resource}"
            )
