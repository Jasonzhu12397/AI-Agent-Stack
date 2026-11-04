import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


class CapiPivotError(ValueError):
    pass


def _validate_identifier(value: Any, error_code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 63
        or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", value)
    ):
        raise CapiPivotError(error_code)
    return value


def _resolve_kubeconfig(
    env: dict[str, str],
    variable: str,
    secret_root: Path,
) -> Path:
    raw_path = env.get(variable, "")
    if not raw_path:
        raise CapiPivotError("CAPI_PIVOT_KUBECONFIG_NOT_CONFIGURED")
    try:
        path = Path(raw_path).resolve(strict=True)
        root = secret_root.resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise CapiPivotError("CAPI_PIVOT_KUBECONFIG_PATH_INVALID") from exc
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise CapiPivotError("CAPI_PIVOT_KUBECONFIG_INVALID")
    return path


def _safe_output(value: str, limit: int = 12000) -> str:
    value = re.sub(
        r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+",
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(
        r"(?i)(password|token|client-key-data|client-certificate-data)\s*[:=]\s*[^\s,]+",
        r"\1=[REDACTED]",
        value,
    )
    return value[-limit:]


def _verify_source_namespace_scope(
    runner,
    kubectl: str,
    source_kubeconfig: Path,
    namespace: str,
    cluster_name: str,
    process_env: dict[str, str],
) -> dict[str, Any]:
    command = [
        kubectl,
        "--kubeconfig",
        str(source_kubeconfig),
        "--namespace",
        namespace,
        "get",
        "clusters.cluster.x-k8s.io",
        "-o",
        "json",
    ]
    try:
        completed = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            env=process_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise CapiPivotError("CAPI_PIVOT_SOURCE_SCOPE_TIMEOUT") from exc
    except OSError as exc:
        raise CapiPivotError("CAPI_PIVOT_SOURCE_SCOPE_FAILED") from exc
    if completed.returncode != 0:
        raise CapiPivotError("CAPI_PIVOT_SOURCE_SCOPE_FAILED")
    try:
        document = json.loads(completed.stdout or "")
    except (TypeError, ValueError) as exc:
        raise CapiPivotError("CAPI_PIVOT_SOURCE_SCOPE_INVALID") from exc
    items = document.get("items") if isinstance(document, dict) else None
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise CapiPivotError("CAPI_PIVOT_SOURCE_SCOPE_INVALID")
    names = [
        item.get("metadata", {}).get("name")
        for item in items
        if isinstance(item.get("metadata"), dict)
    ]
    if len(names) != len(items):
        raise CapiPivotError("CAPI_PIVOT_SOURCE_SCOPE_INVALID")
    if names != [cluster_name]:
        raise CapiPivotError("CAPI_PIVOT_NAMESPACE_MUST_CONTAIN_ONLY_TARGET_CLUSTER")
    return {"source_cluster_count": len(items), "source_cluster_name": cluster_name}


def run_capi_pivot_dry_run(
    payload: dict[str, Any],
    environ: dict[str, str] | None = None,
    executable_lookup=None,
    process_runner=None,
) -> dict[str, Any]:
    env = dict(os.environ if environ is None else environ)
    lookup = executable_lookup or shutil.which
    runner = process_runner or subprocess.run
    if set(payload) - {"cluster_name", "namespace", "deployment_id"}:
        raise CapiPivotError("CAPI_PIVOT_PAYLOAD_FIELD_NOT_ALLOWED")
    deployment_id = payload.get("deployment_id")
    if deployment_id is not None and (
        not isinstance(deployment_id, str) or len(deployment_id) > 64
    ):
        raise CapiPivotError("INVALID_DEPLOYMENT_ID")

    cluster_name = _validate_identifier(payload.get("cluster_name"), "INVALID_CLUSTER_NAME")
    namespace = _validate_identifier(payload.get("namespace", "default"), "INVALID_NAMESPACE")

    secret_root = Path(
        env.get("CAPI_PIVOT_SECRET_DIR", "/var/run/secrets/ai-agent/capi-pivot")
    )
    source_kubeconfig = _resolve_kubeconfig(
        env,
        "CAPI_PIVOT_SOURCE_KUBECONFIG",
        secret_root,
    )
    target_kubeconfig = _resolve_kubeconfig(
        env,
        "CAPI_PIVOT_TARGET_KUBECONFIG",
        secret_root,
    )
    if source_kubeconfig == target_kubeconfig:
        raise CapiPivotError("CAPI_PIVOT_KUBECONFIGS_MUST_DIFFER")
    kubectl = lookup("kubectl")
    if not kubectl:
        raise CapiPivotError("KUBECTL_NOT_INSTALLED")
    clusterctl = lookup("clusterctl")
    if not clusterctl:
        raise CapiPivotError("CLUSTERCTL_NOT_INSTALLED")

    process_env = {
        "PATH": env.get("PATH", ""),
        "HOME": env.get("HOME", "/tmp"),
    }
    source_scope = _verify_source_namespace_scope(
        runner,
        kubectl,
        source_kubeconfig,
        namespace,
        cluster_name,
        process_env,
    )
    command = [
        clusterctl,
        "move",
        "--dry-run",
        "--to-kubeconfig",
        str(target_kubeconfig),
        "--namespace",
        namespace,
        "-v",
        "5",
    ]
    process_env["KUBECONFIG"] = str(source_kubeconfig)
    try:
        completed = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
            env=process_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise CapiPivotError("CAPI_PIVOT_DRY_RUN_TIMEOUT") from exc
    except OSError as exc:
        raise CapiPivotError("CAPI_PIVOT_DRY_RUN_EXECUTION_FAILED") from exc

    stdout = _safe_output(completed.stdout or "")
    stderr = _safe_output(completed.stderr or "")
    if completed.returncode != 0:
        raise CapiPivotError("CAPI_PIVOT_DRY_RUN_FAILED")

    return {
        "cluster_name": cluster_name,
        "namespace": namespace,
        "dry_run_passed": True,
        "execution_mode": "clusterctl_move_dry_run",
        "source_kubeconfig_configured": True,
        "target_kubeconfig_configured": True,
        "source_cluster_count": source_scope["source_cluster_count"],
        "namespace_scope": "all_capi_objects_in_namespace",
        "namespace_scope_warning": True,
        "status_subresources_migrated": False,
        "side_effects": False,
        "exit_code": completed.returncode,
        "stdout_summary": stdout,
        "stderr_summary": stderr,
    }



def _kubectl_json(runner, kubectl, kubeconfig, args, process_env, error_code):
    command = [kubectl, "--kubeconfig", str(kubeconfig), *args]
    try:
        completed = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            env=process_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CapiPivotError(error_code) from exc
    if completed.returncode != 0:
        raise CapiPivotError(error_code)
    try:
        document = json.loads(completed.stdout or "")
    except (TypeError, ValueError) as exc:
        raise CapiPivotError(error_code) from exc
    if not isinstance(document, dict):
        raise CapiPivotError(error_code)
    return document


def _verify_target_management_plane(
    runner,
    kubectl,
    kubeconfig,
    namespace,
    cluster_name,
    process_env,
):
    discovery = _kubectl_json(
        runner,
        kubectl,
        kubeconfig,
        ["get", "--raw", "/apis"],
        process_env,
        "CAPI_PIVOT_TARGET_API_DISCOVERY_FAILED",
    )
    groups = {
        item.get("name"): item
        for item in discovery.get("groups", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    required_resources = {
        "cluster.x-k8s.io": {
            "clusters", "machines", "machinesets", "machinedeployments"
        },
        "bootstrap.cluster.x-k8s.io": {
            "kubeadmconfigs", "kubeadmconfigtemplates"
        },
        "controlplane.cluster.x-k8s.io": {"kubeadmcontrolplanes"},
        "infrastructure.cluster.x-k8s.io": {
            "metal3clusters", "metal3machines", "metal3machinetemplates"
        },
        "metal3.io": {"baremetalhosts"},
    }
    versions = {}
    for group_name, expected in required_resources.items():
        group = groups.get(group_name)
        version = (
            group.get("preferredVersion", {}).get("version")
            if isinstance(group, dict)
            else None
        )
        if not isinstance(version, str) or not version:
            raise CapiPivotError("CAPI_PIVOT_TARGET_APIS_NOT_READY")
        version_data = _kubectl_json(
            runner,
            kubectl,
            kubeconfig,
            ["get", "--raw", f"/apis/{group_name}/{version}"],
            process_env,
            "CAPI_PIVOT_TARGET_API_DISCOVERY_FAILED",
        )
        available = {
            item.get("name")
            for item in version_data.get("resources", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        if not expected.issubset(available):
            raise CapiPivotError("CAPI_PIVOT_TARGET_APIS_NOT_READY")
        versions[group_name] = version

    controllers = {
        "capi-system": "capi-controller-manager",
        "capi-kubeadm-bootstrap-system": "capi-kubeadm-bootstrap-controller-manager",
        "capi-kubeadm-control-plane-system": "capi-kubeadm-control-plane-controller-manager",
        "capm3-system": "capm3-controller-manager",
    }
    for controller_namespace, deployment_name in controllers.items():
        deployment = _kubectl_json(
            runner,
            kubectl,
            kubeconfig,
            [
                "get",
                "deployment",
                deployment_name,
                "--namespace",
                controller_namespace,
                "-o",
                "json",
            ],
            process_env,
            "CAPI_PIVOT_TARGET_CONTROLLERS_NOT_READY",
        )
        desired = deployment.get("spec", {}).get("replicas", 1)
        available = deployment.get("status", {}).get("availableReplicas", 0)
        observed = deployment.get("status", {}).get("observedGeneration", 0)
        generation = deployment.get("metadata", {}).get("generation", 1)
        if (
            not isinstance(desired, int)
            or isinstance(desired, bool)
            or desired < 1
            or not isinstance(available, int)
            or isinstance(available, bool)
            or available < desired
            or not isinstance(observed, int)
            or not isinstance(generation, int)
            or observed < generation
        ):
            raise CapiPivotError("CAPI_PIVOT_TARGET_CONTROLLERS_NOT_READY")

    cluster_list = _kubectl_json(
        runner,
        kubectl,
        kubeconfig,
        [
            "--namespace",
            namespace,
            "get",
            "clusters.cluster.x-k8s.io",
            "-o",
            "json",
        ],
        process_env,
        "CAPI_PIVOT_TARGET_CLUSTER_CHECK_FAILED",
    )
    items = cluster_list.get("items")
    if not isinstance(items, list):
        raise CapiPivotError("CAPI_PIVOT_TARGET_CLUSTER_CHECK_FAILED")
    if any(
        isinstance(item, dict)
        and item.get("metadata", {}).get("name") == cluster_name
        for item in items
    ):
        raise CapiPivotError("CAPI_PIVOT_TARGET_CLUSTER_NAME_CONFLICT")
    return {
        "provider_versions": versions,
        "controllers_ready": True,
        "target_cluster_name_available": True,
    }


def run_capi_pivot(
    payload: dict[str, Any],
    environ: dict[str, str] | None = None,
    executable_lookup=None,
    process_runner=None,
) -> dict[str, Any]:
    env = dict(os.environ if environ is None else environ)
    lookup = executable_lookup or shutil.which
    runner = process_runner or subprocess.run

    dry_run = run_capi_pivot_dry_run(
        payload,
        environ=env,
        executable_lookup=lookup,
        process_runner=runner,
    )
    secret_root = Path(
        env.get("CAPI_PIVOT_SECRET_DIR", "/var/run/secrets/ai-agent/capi-pivot")
    )
    target_kubeconfig = _resolve_kubeconfig(
        env,
        "CAPI_PIVOT_TARGET_KUBECONFIG",
        secret_root,
    )
    clusterctl = lookup("clusterctl")
    kubectl = lookup("kubectl")
    if not clusterctl:
        raise CapiPivotError("CLUSTERCTL_NOT_INSTALLED")
    if not kubectl:
        raise CapiPivotError("KUBECTL_NOT_INSTALLED")

    cluster_name = dry_run["cluster_name"]
    namespace = dry_run["namespace"]
    target_process_env = {
        "PATH": env.get("PATH", ""),
        "HOME": env.get("HOME", "/tmp"),
    }
    target_preflight = _verify_target_management_plane(
        runner,
        kubectl,
        target_kubeconfig,
        namespace,
        cluster_name,
        target_process_env,
    )
    command = [
        clusterctl,
        "move",
        "--to-kubeconfig",
        str(target_kubeconfig),
        "--namespace",
        namespace,
        "-v",
        "5",
    ]
    process_env = {
        "PATH": env.get("PATH", ""),
        "HOME": env.get("HOME", "/tmp"),
        "KUBECONFIG": env["CAPI_PIVOT_SOURCE_KUBECONFIG"],
    }
    try:
        completed = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=1800,
            stdin=subprocess.DEVNULL,
            env=process_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise CapiPivotError("CAPI_PIVOT_EXECUTION_TIMEOUT_OUTCOME_UNKNOWN") from exc
    except OSError as exc:
        raise CapiPivotError("CAPI_PIVOT_EXECUTION_FAILED") from exc

    stdout = _safe_output(completed.stdout or "")
    stderr = _safe_output(completed.stderr or "")
    if completed.returncode != 0:
        raise CapiPivotError("CAPI_PIVOT_EXECUTION_FAILED")

    return {
        "cluster_name": cluster_name,
        "namespace": namespace,
        "pivot_executed": True,
        "execution_mode": "clusterctl_move",
        "dry_run_passed": True,
        "source_namespace_scope_verified": True,
        "target_management_plane_verified": True,
        "target_provider_versions": target_preflight["provider_versions"],
        "target_controllers_ready": target_preflight["controllers_ready"],
        "target_cluster_name_available": target_preflight["target_cluster_name_available"],
        "side_effects": True,
        "automatic_retry_allowed": False,
        "exit_code": completed.returncode,
        "stdout_summary": stdout,
        "stderr_summary": stderr,
    }
