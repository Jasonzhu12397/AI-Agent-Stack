"""Install the Target Agent into the new Target Control Plane from a sealed bundle.

The EPH Agent owns this step because it is the only agent that runs before the
Target Control Plane is ready and already holds the Pivot target kubeconfig.
The bundle is provisioned by the platform as a Kubernetes Secret in the EPH
cluster; it is never sent through a command payload and its contents are never
written to the command result.

The executor is deliberately narrow: it accepts only the workload and RBAC
kinds a Target Agent bootstrap needs, rejects hostPath volumes, applies the
bundle idempotently with server-side apply, and verifies that the rollout and
the Target cluster identity are real before reporting success.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

BUNDLE_DIR_ENV = "TARGET_AGENT_BOOTSTRAP_DIR"
DEFAULT_BUNDLE_DIR = Path("/var/run/secrets/ai-agent/target-agent-bootstrap")
BUNDLE_FILE = "bundle.yaml"
TARGET_KUBECONFIG_ENV = "CAPI_PIVOT_TARGET_KUBECONFIG"
DEFAULT_TARGET_KUBECONFIG = Path(
    "/var/run/secrets/ai-agent/capi-pivot/target/kubeconfig"
)
AGENT_NAMESPACE = "ai-agent-system"
AGENT_DEPLOYMENT = "target-agent"
CREDENTIALS_SECRET = "target-agent-credentials"
MTLS_SECRET = "agent-control-plane-mtls"
REQUIRED_CREDENTIAL_KEYS = frozenset(
    {
        "control-url",
        "agent-id",
        "installation-id",
        "cluster-id",
        "ironic-api-url",
        "ironic-api-token",
        "ironic-internal-ntp-ip",
        "approved-external-ntp-sources",
        "worker-network-cidrs",
        "required-controller-deployments",
    }
)
REQUIRED_MTLS_KEYS = frozenset({"ca.crt", "tls.crt", "tls.key"})
ALLOWED_KINDS = frozenset(
    {
        "ServiceAccount",
        "Secret",
        "ConfigMap",
        "Role",
        "RoleBinding",
        "ClusterRole",
        "ClusterRoleBinding",
        "Deployment",
    }
)
CLUSTER_SCOPED_KINDS = frozenset({"ClusterRole", "ClusterRoleBinding"})
FORBIDDEN_VOLUME_KINDS = frozenset({"hostPath", "nfs", "iscsi", "fc", "csi"})


class TargetBootstrapError(RuntimeError):
    """Bootstrap bundle validation or a Target cluster operation failed."""

    def __init__(self, code: str, side_effects: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.side_effects = side_effects


def _bundle_path(environ: Any) -> Path:
    values = os.environ if environ is None else environ
    configured = values.get(BUNDLE_DIR_ENV)
    directory = Path(configured) if configured else DEFAULT_BUNDLE_DIR
    return directory / BUNDLE_FILE


def _target_kubeconfig(environ: Any) -> Path:
    values = os.environ if environ is None else environ
    configured = values.get(TARGET_KUBECONFIG_ENV)
    return Path(configured) if configured else DEFAULT_TARGET_KUBECONFIG


def _load_documents(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_BUNDLE_REQUIRED")
    try:
        documents = [
            document
            for document in yaml.safe_load_all(path.read_text(encoding="utf-8"))
            if document
        ]
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_BUNDLE_INVALID") from exc
    if not documents:
        raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_BUNDLE_EMPTY")
    return documents


def _metadata(document: dict[str, Any]) -> dict[str, Any]:
    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_METADATA_INVALID")
    return metadata


def _validate_document(document: dict[str, Any]) -> None:
    if not isinstance(document, dict):
        raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_DOCUMENT_INVALID")
    kind = document.get("kind")
    if kind not in ALLOWED_KINDS:
        raise TargetBootstrapError(f"TARGET_AGENT_BOOTSTRAP_KIND_FORBIDDEN:{kind}")
    metadata = _metadata(document)
    name = metadata.get("name")
    if not isinstance(name, str) or not name:
        raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_NAME_REQUIRED")
    if kind not in CLUSTER_SCOPED_KINDS:
        namespace = metadata.get("namespace")
        if namespace != AGENT_NAMESPACE:
            raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_NAMESPACE_INVALID")
    if kind != "Deployment":
        return
    spec = document.get("spec")
    template = spec.get("template") if isinstance(spec, dict) else None
    pod_spec = template.get("spec") if isinstance(template, dict) else None
    if not isinstance(pod_spec, dict):
        raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_DEPLOYMENT_SPEC_INVALID")
    security_context = pod_spec.get("securityContext", {})
    container = pod_spec.get("containers")
    if not isinstance(container, list) or not container:
        raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_CONTAINER_REQUIRED")
    for item in container:
        if not isinstance(item, dict):
            raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_CONTAINER_INVALID")
        item_security = item.get("securityContext", {})
        if item_security.get("privileged") is True:
            raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_PRIVILEGED_CONTAINER")
    if security_context.get("runAsNonRoot") is not True:
        raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_NON_ROOT_REQUIRED")
    for volume in pod_spec.get("volumes", []):
        if not isinstance(volume, dict):
            raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_VOLUME_INVALID")
        for kind in FORBIDDEN_VOLUME_KINDS:
            if kind in volume:
                raise TargetBootstrapError(
                    f"TARGET_AGENT_BOOTSTRAP_VOLUME_FORBIDDEN:{kind}"
                )


def _validate_bundle(documents: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        _validate_document(document)
        by_kind.setdefault(document["kind"], []).append(document)

    deployments = [
        document
        for document in by_kind.get("Deployment", [])
        if _metadata(document).get("name") == AGENT_DEPLOYMENT
    ]
    if len(deployments) != 1:
        raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_DEPLOYMENT_MISSING")
    credentials = [
        document
        for document in by_kind.get("Secret", [])
        if _metadata(document).get("name") == CREDENTIALS_SECRET
    ]
    if len(credentials) != 1:
        raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_CREDENTIALS_MISSING")
    credentials_data = credentials[0].get("stringData", credentials[0].get("data", {}))
    if not isinstance(credentials_data, dict):
        raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_CREDENTIALS_INVALID")
    missing = sorted(REQUIRED_CREDENTIAL_KEYS - set(credentials_data))
    if missing:
        raise TargetBootstrapError(
            "TARGET_AGENT_BOOTSTRAP_CREDENTIALS_INCOMPLETE:" + ",".join(missing)
        )

    mtls = [
        document
        for document in by_kind.get("Secret", [])
        if _metadata(document).get("name") == MTLS_SECRET
    ]
    if len(mtls) != 1:
        raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_MTLS_MISSING")
    mtls_data = mtls[0].get("stringData", mtls[0].get("data", {}))
    if not isinstance(mtls_data, dict):
        raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_MTLS_INVALID")
    missing = sorted(REQUIRED_MTLS_KEYS - set(mtls_data))
    if missing:
        raise TargetBootstrapError(
            "TARGET_AGENT_BOOTSTRAP_MTLS_INCOMPLETE:" + ",".join(missing)
        )
    deployment_spec = deployments[0].get("spec")
    replicas = deployment_spec.get("replicas", 3) if isinstance(deployment_spec, dict) else 3
    if not isinstance(replicas, int) or isinstance(replicas, bool) or replicas < 1:
        raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_REPLICAS_INVALID")
    return {
        "document_count": len(documents),
        "kinds": sorted(by_kind),
        "replicas": replicas,
    }


def _run(
    runner,
    arguments: list[str],
    kubeconfig: Path,
    lease_lost_event: Any,
    timeout: int = 60,
    side_effects: bool = False,
) -> dict[str, Any]:
    if lease_lost_event is not None and lease_lost_event.is_set():
        raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_COMMAND_LEASE_LOST")
    result = runner(
        arguments,
        timeout=timeout,
        max_output_bytes=8 * 1024 * 1024,
        kubeconfig=str(kubeconfig),
        lease_lost_event=lease_lost_event,
    )
    if not isinstance(result, dict) or result.get("available") is not True:
        raise TargetBootstrapError(
            "TARGET_AGENT_BOOTSTRAP_KUBERNETES_API_FAILED", side_effects
        )
    return result


def _json(result: dict[str, Any], code: str, side_effects: bool = False) -> dict[str, Any]:
    try:
        value = json.loads(result.get("stdout", ""))
    except (TypeError, ValueError) as exc:
        raise TargetBootstrapError(code, side_effects) from exc
    if not isinstance(value, dict):
        raise TargetBootstrapError(code, side_effects)
    return value


def execute_target_agent_bootstrap(
    payload: dict[str, Any],
    *,
    runner,
    lease_lost_event: Any = None,
    environ: Any = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_PAYLOAD_INVALID")
    bundle_path = _bundle_path(environ)
    kubeconfig = _target_kubeconfig(environ)
    if not kubeconfig.is_file():
        raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_TARGET_KUBECONFIG_REQUIRED")
    documents = _load_documents(bundle_path)
    summary = _validate_bundle(documents)

    bundle_digest = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    if payload.get("bundle_sha256") not in (None, bundle_digest):
        raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_BUNDLE_DIGEST_MISMATCH")

    path = ""
    applied = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".yaml",
            prefix="target-agent-bootstrap-",
            delete=False,
            encoding="utf-8",
        ) as handle:
            path = handle.name
            yaml.safe_dump_all(documents, handle, sort_keys=False)
        os.chmod(path, 0o600)
        applied = True
        try:
            _run(
                runner,
                ["apply", "--server-side", "--field-manager=ai-agent-eph-agent", "-f", path],
                kubeconfig,
                lease_lost_event,
                timeout=180,
            )
        except TargetBootstrapError as exc:
            raise TargetBootstrapError(exc.code, True) from exc
    finally:
        if path:
            Path(path).unlink(missing_ok=True)

    rollout = _run(
        runner,
        [
            "rollout",
            "status",
            f"deployment/{AGENT_DEPLOYMENT}",
            "--namespace",
            AGENT_NAMESPACE,
            "--timeout=300s",
        ],
        kubeconfig,
        lease_lost_event,
        timeout=330,
        side_effects=True,
    )
    if rollout.get("returncode") != 0:
        raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_ROLLOUT_FAILED", applied)

    namespace = _json(
        _run(
            runner,
            ["get", "namespace", "kube-system", "-o", "json"],
            kubeconfig,
            lease_lost_event,
            side_effects=True,
        ),
        "TARGET_AGENT_BOOTSTRAP_TARGET_CLUSTER_INVALID",
        True,
    )
    target_cluster_uid = namespace.get("metadata", {}).get("uid")
    if not isinstance(target_cluster_uid, str) or not target_cluster_uid:
        raise TargetBootstrapError(
            "TARGET_AGENT_BOOTSTRAP_TARGET_CLUSTER_UID_MISSING", applied
        )

    deployment = _json(
        _run(
            runner,
            [
                "get",
                f"deployment/{AGENT_DEPLOYMENT}",
                "--namespace",
                AGENT_NAMESPACE,
                "-o",
                "json",
            ],
            kubeconfig,
            lease_lost_event,
            side_effects=True,
        ),
        "TARGET_AGENT_BOOTSTRAP_DEPLOYMENT_INVALID",
        True,
    )
    status = deployment.get("status", {})
    ready_replicas = status.get("readyReplicas", 0)
    if not isinstance(ready_replicas, int) or ready_replicas < summary["replicas"]:
        raise TargetBootstrapError("TARGET_AGENT_BOOTSTRAP_NOT_READY", applied)

    return {
        "target_agent_bootstrapped": True,
        "bundle_sha256": bundle_digest,
        "document_count": summary["document_count"],
        "kinds": summary["kinds"],
        "target_cluster_uid": target_cluster_uid,
        "deployment": f"{AGENT_NAMESPACE}/{AGENT_DEPLOYMENT}",
        "ready_replicas": ready_replicas,
        "side_effects": True,
    }
