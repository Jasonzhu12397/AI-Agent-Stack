import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any


class CapiPivotError(ValueError):
    pass


def _raise_if_cancelled(cancellation_event, error_code: str) -> None:
    if cancellation_event is not None and cancellation_event.is_set():
        raise CapiPivotError(error_code)


def _cancellable_process_runner(cancellation_event):
    def run(command, check=False, capture_output=False, text=False, timeout=None,
            stdin=None, env=None):
        _raise_if_cancelled(
            cancellation_event,
            "CAPI_PIVOT_CANCELLED_BEFORE_PROCESS",
        )
        if not capture_output:
            raise CapiPivotError("CAPI_PIVOT_PROCESS_CAPTURE_REQUIRED")
        process = subprocess.Popen(
            command,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
            env=env,
            start_new_session=True,
        )
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = process.communicate()
                raise CapiPivotError("CAPI_PIVOT_CANCELLED_DURING_PROCESS")
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = process.communicate()
                raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
            wait_time = 0.25 if remaining is None else min(0.25, remaining)
            try:
                stdout, stderr = process.communicate(timeout=wait_time)
                break
            except subprocess.TimeoutExpired:
                continue
        completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if check and completed.returncode != 0:
            raise subprocess.CalledProcessError(
                completed.returncode, command, output=stdout, stderr=stderr
            )
        return completed

    return run


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
    api_groups = discovery.get("groups")
    if not isinstance(api_groups, list) or any(
        not isinstance(item, dict) for item in api_groups
    ):
        raise CapiPivotError("CAPI_PIVOT_TARGET_API_DISCOVERY_FAILED")
    groups = {
        item.get("name"): item
        for item in api_groups
        if isinstance(item.get("name"), str)
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
        preferred = group.get("preferredVersion") if isinstance(group, dict) else None
        version = preferred.get("version") if isinstance(preferred, dict) else None
        if not isinstance(version, str) or not re.fullmatch(
            r"v[0-9]+(?:alpha|beta)[0-9]+|v[0-9]+", version
        ):
            raise CapiPivotError("CAPI_PIVOT_TARGET_APIS_NOT_READY")
        version_data = _kubectl_json(
            runner,
            kubectl,
            kubeconfig,
            ["get", "--raw", f"/apis/{group_name}/{version}"],
            process_env,
            "CAPI_PIVOT_TARGET_API_DISCOVERY_FAILED",
        )
        api_resources = version_data.get("resources")
        if not isinstance(api_resources, list) or any(
            not isinstance(item, dict) for item in api_resources
        ):
            raise CapiPivotError("CAPI_PIVOT_TARGET_API_DISCOVERY_FAILED")
        available = {
            item.get("name")
            for item in api_resources
            if isinstance(item.get("name"), str)
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
        deployment_spec = deployment.get("spec")
        deployment_status = deployment.get("status")
        deployment_metadata = deployment.get("metadata")
        if not all(
            isinstance(value, dict)
            for value in (deployment_spec, deployment_status, deployment_metadata)
        ):
            raise CapiPivotError("CAPI_PIVOT_TARGET_CONTROLLERS_NOT_READY")
        desired = deployment_spec.get("replicas", 1)
        available = deployment_status.get("availableReplicas", 0)
        observed = deployment_status.get("observedGeneration", 0)
        generation = deployment_metadata.get("generation", 1)
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
    if not isinstance(items, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("metadata"), dict)
        for item in items
    ):
        raise CapiPivotError("CAPI_PIVOT_TARGET_CLUSTER_CHECK_FAILED")
    if any(
        item["metadata"].get("name") == cluster_name
        for item in items
    ):
        raise CapiPivotError("CAPI_PIVOT_TARGET_CLUSTER_NAME_CONFLICT")
    return {
        "provider_versions": versions,
        "controllers_ready": True,
        "target_cluster_name_available": True,
    }



def _kubectl_optional_json(
    runner,
    kubectl,
    kubeconfig,
    args,
    process_env,
    error_code,
):
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
        if "notfound" in (completed.stderr or "").lower():
            return None
        raise CapiPivotError(error_code)
    try:
        document = json.loads(completed.stdout or "")
    except (TypeError, ValueError) as exc:
        raise CapiPivotError(error_code) from exc
    if not isinstance(document, dict):
        raise CapiPivotError(error_code)
    return document


def _verify_source_metal3_handoff(
    runner,
    kubectl,
    source_kubeconfig,
    target_kubeconfig,
    namespace,
    cluster_name,
    source_env,
    target_env,
):
    host_list = _kubectl_json(
        runner,
        kubectl,
        source_kubeconfig,
        [
            "--namespace",
            namespace,
            "get",
            "baremetalhosts.metal3.io",
            "-o",
            "json",
        ],
        source_env,
        "CAPI_PIVOT_SOURCE_BMH_INVENTORY_FAILED",
    )
    hosts = host_list.get("items")
    if not isinstance(hosts, list):
        raise CapiPivotError("CAPI_PIVOT_SOURCE_BMH_INVENTORY_INVALID")

    stable_states = {
        "available",
        "provisioned",
        "externally provisioned",
        "deprovisioned",
        "ready",
    }
    selected_hosts = []
    blockers = []
    for host in hosts:
        if not isinstance(host, dict):
            raise CapiPivotError("CAPI_PIVOT_SOURCE_BMH_INVENTORY_INVALID")
        metadata = host.get("metadata", {})
        if not isinstance(metadata, dict):
            raise CapiPivotError("CAPI_PIVOT_SOURCE_BMH_INVENTORY_INVALID")
        labels = metadata.get("labels", {})
        annotations = metadata.get("annotations", {})
        spec = host.get("spec", {})
        consumer = spec.get("consumerRef")
        associated = (
            isinstance(consumer, dict)
            and consumer.get("kind") == "Metal3Machine"
        ) or (
            isinstance(labels, dict)
            and labels.get("cluster.x-k8s.io/cluster-name") == cluster_name
        )
        if not associated:
            continue
        name = metadata.get("name")
        if not isinstance(name, str) or not name:
            raise CapiPivotError("CAPI_PIVOT_SOURCE_BMH_IDENTITY_INVALID")
        if not isinstance(annotations, dict):
            raise CapiPivotError("CAPI_PIVOT_SOURCE_BMH_ANNOTATIONS_INVALID")
        if "clusterctl.cluster.x-k8s.io/block-move" in annotations:
            blockers.append("bmh_block_move_annotation_present:" + name)
        status = host.get("status", {})
        if not isinstance(status, dict):
            raise CapiPivotError("CAPI_PIVOT_SOURCE_BMH_STATUS_INVALID")
        move_labels = {
            "clusterctl.cluster.x-k8s.io/move",
            "clusterctl.cluster.x-k8s.io/move-hierarchy",
        }
        if not isinstance(labels, dict) or not move_labels.intersection(labels):
            blockers.append("bmh_clusterctl_move_label_missing:" + name)
        pause_annotation = annotations.get("baremetalhost.metal3.io/paused")
        if pause_annotation is None:
            blockers.append("bmh_pause_annotation_missing:" + name)
        elif pause_annotation != "metal3.io/capm3":
            blockers.append("bmh_pause_annotation_value_invalid:" + name)
        status_annotation = annotations.get("baremetalhost.metal3.io/status")
        if not isinstance(status_annotation, str) or not status_annotation:
            blockers.append("bmh_status_annotation_missing:" + name)
        else:
            try:
                annotated_status = json.loads(status_annotation)
            except (TypeError, ValueError):
                annotated_status = None
            expected_status = dict(status)
            if not isinstance(annotated_status, dict) or annotated_status != expected_status:
                blockers.append("bmh_status_annotation_mismatch:" + name)
        provisioning = status.get("provisioning", {})
        state = provisioning.get("state") if isinstance(provisioning, dict) else None
        operational_status = status.get("operationalStatus") if isinstance(status, dict) else None
        if state not in stable_states:
            blockers.append("bmh_not_in_stable_provisioning_state:" + name)
        if operational_status not in ("OK", "detached"):
            blockers.append("bmh_operational_status_not_healthy:" + name)
        if status.get("errorMessage") or status.get("errorType"):
            blockers.append("bmh_status_contains_error:" + name)
        selected_hosts.append(
            {
                "name": name,
                "namespace": metadata.get("namespace"),
                "provisioning_state": state,
                "operational_status": operational_status,
            }
        )

    if blockers:
        raise CapiPivotError("CAPI_PIVOT_SOURCE_BMH_NOT_SAFE:" + ",".join(blockers[:20]))

    source_ironic = _kubectl_optional_json(
        runner,
        kubectl,
        source_kubeconfig,
        [
            "--namespace",
            namespace,
            "get",
            "deployment",
            "metal3-ironic",
            "-o",
            "json",
        ],
        source_env,
        "CAPI_PIVOT_SOURCE_IRONIC_STATE_UNVERIFIED",
    )
    if source_ironic is None:
        if source_env.get("CAPI_PIVOT_SOURCE_EXTERNAL_IRONIC_HANDOFF_CONFIRMED") != "true":
            raise CapiPivotError("CAPI_PIVOT_SOURCE_EXTERNAL_IRONIC_HANDOFF_NOT_CONFIRMED")
        source_ironic_state = "external_handoff_attested"
    else:
        metadata = source_ironic.get("metadata", {})
        spec = source_ironic.get("spec", {})
        status = source_ironic.get("status", {})
        generation = metadata.get("generation") if isinstance(metadata, dict) else None
        observed = status.get("observedGeneration") if isinstance(status, dict) else None
        replicas = spec.get("replicas") if isinstance(spec, dict) else None
        running_counts = (
            status.get("replicas", 0),
            status.get("readyReplicas", 0),
            status.get("updatedReplicas", 0),
            status.get("availableReplicas", 0),
            status.get("terminatingReplicas", 0),
        ) if isinstance(status, dict) else None
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
            or not isinstance(observed, int)
            or isinstance(observed, bool)
            or observed < generation
            or not isinstance(replicas, int)
            or isinstance(replicas, bool)
            or replicas != 0
            or not isinstance(running_counts, tuple)
            or any(
                not isinstance(count, int)
                or isinstance(count, bool)
                or count != 0
                for count in running_counts
            )
        ):
            raise CapiPivotError("CAPI_PIVOT_SOURCE_IRONIC_MUST_BE_STOPPED")
        source_ironic_state = "deployment_scaled_to_zero_and_observed"

    target_ironic = _kubectl_optional_json(
        runner,
        kubectl,
        target_kubeconfig,
        [
            "--namespace",
            namespace,
            "get",
            "deployment",
            "metal3-ironic",
            "-o",
            "json",
        ],
        target_env,
        "CAPI_PIVOT_TARGET_IRONIC_STATE_UNVERIFIED",
    )
    if target_ironic is None:
        if target_env.get("CAPI_PIVOT_TARGET_EXTERNAL_IRONIC_READY_CONFIRMED") != "true":
            raise CapiPivotError("CAPI_PIVOT_TARGET_EXTERNAL_IRONIC_NOT_CONFIRMED")
        target_ironic_state = "external_endpoint_readiness_attested"
    else:
        metadata = target_ironic.get("metadata", {})
        spec = target_ironic.get("spec", {})
        status = target_ironic.get("status", {})
        template = spec.get("template", {}) if isinstance(spec, dict) else None
        pod_spec = template.get("spec", {}) if isinstance(template, dict) else None
        containers = pod_spec.get("containers", []) if isinstance(pod_spec, dict) else None
        if (
            not isinstance(metadata, dict)
            or not isinstance(spec, dict)
            or not isinstance(status, dict)
            or not isinstance(pod_spec, dict)
            or not isinstance(containers, list)
        ):
            raise CapiPivotError("CAPI_PIVOT_TARGET_IRONIC_STATE_INVALID")
        container_names = {
            item.get("name")
            for item in containers
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        desired = spec.get("replicas", 1)
        available = status.get("availableReplicas", 0)
        observed = status.get("observedGeneration", 0)
        generation = metadata.get("generation")
        if (
            not isinstance(desired, int)
            or isinstance(desired, bool)
            or desired < 1
            or not isinstance(available, int)
            or isinstance(available, bool)
            or available < desired
            or not isinstance(observed, int)
            or isinstance(observed, bool)
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
            or observed < generation
        ):
            raise CapiPivotError("CAPI_PIVOT_TARGET_IRONIC_NOT_READY")
        if "ironic-endpoint-keepalived" not in container_names:
            raise CapiPivotError("CAPI_PIVOT_TARGET_IRONIC_KEEPALIVED_MISSING")
        target_ironic_state = "deployment_ready_with_keepalived"

    return {
        "associated_bmh_count": len(selected_hosts),
        "associated_bmhs": selected_hosts[:200],
        "all_associated_bmhs_stable": True,
        "source_ironic_handoff": source_ironic_state,
        "target_ironic_handoff": target_ironic_state,
        "provisioning_endpoint_handoff_verified": True,
    }

def run_capi_pivot(
    payload: dict[str, Any],
    environ: dict[str, str] | None = None,
    executable_lookup=None,
    process_runner=None,
    cancellation_event=None,
) -> dict[str, Any]:
    _raise_if_cancelled(cancellation_event, "CAPI_PIVOT_CANCELLED_BEFORE_EXECUTION")
    env = dict(os.environ if environ is None else environ)
    lookup = executable_lookup or shutil.which
    runner = process_runner or _cancellable_process_runner(cancellation_event)

    def preflight_runner(*args, **kwargs):
        _raise_if_cancelled(
            cancellation_event,
            "CAPI_PIVOT_CANCELLED_DURING_PREFLIGHT",
        )
        completed = runner(*args, **kwargs)
        _raise_if_cancelled(
            cancellation_event,
            "CAPI_PIVOT_CANCELLED_DURING_PREFLIGHT",
        )
        return completed

    dry_run = run_capi_pivot_dry_run(
        payload,
        environ=env,
        executable_lookup=lookup,
        process_runner=preflight_runner,
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
        preflight_runner,
        kubectl,
        target_kubeconfig,
        namespace,
        cluster_name,
        target_process_env,
    )
    source_kubeconfig = _resolve_kubeconfig(
        env,
        "CAPI_PIVOT_SOURCE_KUBECONFIG",
        secret_root,
    )
    metal3_handoff = _verify_source_metal3_handoff(
        preflight_runner,
        kubectl,
        source_kubeconfig,
        target_kubeconfig,
        namespace,
        cluster_name,
        env,
        env,
    )
    _raise_if_cancelled(
        cancellation_event,
        "CAPI_PIVOT_CANCELLED_BEFORE_SIMULATION",
    )

    return {
        "cluster_name": cluster_name,
        "namespace": namespace,
        "pivot_executed": False,
        "pivot_simulated": True,
        "execution_mode": "preflight_verified_simulation",
        "simulation_status": "ready_for_target_agent_acknowledgement",
        "dry_run_passed": dry_run["dry_run_passed"],
        "source_namespace_scope_verified": True,
        "target_management_plane_verified": True,
        "target_provider_versions": target_preflight["provider_versions"],
        "target_controllers_ready": target_preflight["controllers_ready"],
        "target_cluster_name_available": target_preflight["target_cluster_name_available"],
        "metal3_handoff": metal3_handoff,
        "ownership_transferred": False,
        "target_agent_acknowledgement_required": True,
        "postflight_required": True,
        "side_effects": False,
        "automatic_retry_allowed": True,
        "exit_code": dry_run["exit_code"],
        "stdout_summary": dry_run["stdout_summary"],
        "stderr_summary": dry_run["stderr_summary"],
    }


def run_capi_pivot_postflight(
    payload: dict[str, Any],
    environ: dict[str, str] | None = None,
    executable_lookup=None,
    process_runner=None,
) -> dict[str, Any]:
    env = dict(os.environ if environ is None else environ)
    if set(payload) - {"cluster_name", "namespace", "baremetal_host_namespace"}:
        raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_PAYLOAD_FIELD_NOT_ALLOWED")
    cluster_name = _validate_identifier(
        payload.get("cluster_name"), "INVALID_CLUSTER_NAME"
    )
    namespace = _validate_identifier(
        payload.get("namespace", "default"), "INVALID_NAMESPACE"
    )
    host_namespace = _validate_identifier(
        payload.get("baremetal_host_namespace", namespace), "INVALID_BAREMETAL_HOST_NAMESPACE"
    )
    secret_root = Path(
        env.get("CAPI_PIVOT_SECRET_DIR", "/var/run/secrets/ai-agent/capi-pivot")
    )
    target_kubeconfig = _resolve_kubeconfig(
        env, "CAPI_PIVOT_TARGET_KUBECONFIG", secret_root
    )
    lookup = executable_lookup or shutil.which
    runner = process_runner or subprocess.run
    kubectl = lookup("kubectl")
    if not kubectl:
        raise CapiPivotError("KUBECTL_NOT_INSTALLED")
    process_env = {
        "PATH": env.get("PATH", ""),
        "HOME": env.get("HOME", "/tmp"),
    }

    cluster_list = _kubectl_json(
        runner,
        kubectl,
        target_kubeconfig,
        [
            "--namespace", namespace, "get", "clusters.cluster.x-k8s.io",
            cluster_name, "-o", "json",
        ],
        process_env,
        "CAPI_PIVOT_POSTFLIGHT_CLUSTER_NOT_FOUND",
    )
    cluster_uid = cluster_list.get("metadata", {}).get("uid")
    if not isinstance(cluster_uid, str) or not cluster_uid:
        raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_CLUSTER_IDENTITY_INVALID")

    required_controllers = {
        "capi-system": "capi-controller-manager",
        "capi-kubeadm-bootstrap-system": "capi-kubeadm-bootstrap-controller-manager",
        "capi-kubeadm-control-plane-system": "capi-kubeadm-control-plane-controller-manager",
        "capm3-system": "capm3-controller-manager",
    }
    controller_evidence = []
    for controller_namespace, deployment_name in required_controllers.items():
        deployment = _kubectl_json(
            runner,
            kubectl,
            target_kubeconfig,
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
            "CAPI_PIVOT_POSTFLIGHT_CONTROLLERS_UNAVAILABLE",
        )
        metadata = deployment.get("metadata", {})
        spec = deployment.get("spec", {})
        status = deployment.get("status", {})
        desired = spec.get("replicas", 1) if isinstance(spec, dict) else None
        available = status.get("availableReplicas", 0) if isinstance(status, dict) else None
        observed = status.get("observedGeneration", 0) if isinstance(status, dict) else None
        generation = metadata.get("generation", 1) if isinstance(metadata, dict) else None
        if (
            not isinstance(desired, int)
            or isinstance(desired, bool)
            or desired < 1
            or not isinstance(available, int)
            or isinstance(available, bool)
            or available < desired
            or not isinstance(observed, int)
            or isinstance(observed, bool)
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or observed < generation
        ):
            raise CapiPivotError(
                "CAPI_PIVOT_POSTFLIGHT_CONTROLLER_NOT_READY:"
                + controller_namespace
                + "/"
                + deployment_name
            )
        controller_evidence.append(
            {
                "namespace": controller_namespace,
                "deployment": deployment_name,
                "desired_replicas": desired,
                "available_replicas": available,
                "observed_generation": observed,
                "generation": generation,
            }
        )

    machine_list = _kubectl_json(
        runner,
        kubectl,
        target_kubeconfig,
        [
            "--namespace", namespace, "get", "machines.cluster.x-k8s.io",
            "-o", "json",
        ],
        process_env,
        "CAPI_PIVOT_POSTFLIGHT_MACHINES_UNAVAILABLE",
    )
    m3m_list = _kubectl_json(
        runner,
        kubectl,
        target_kubeconfig,
        [
            "--namespace", namespace, "get", "metal3machines.infrastructure.cluster.x-k8s.io",
            "-o", "json",
        ],
        process_env,
        "CAPI_PIVOT_POSTFLIGHT_METAL3MACHINES_UNAVAILABLE",
    )
    bmh_list = _kubectl_json(
        runner,
        kubectl,
        target_kubeconfig,
        [
            "--namespace", host_namespace, "get", "baremetalhosts.metal3.io",
            "-o", "json",
        ],
        process_env,
        "CAPI_PIVOT_POSTFLIGHT_BMHS_UNAVAILABLE",
    )
    machines = machine_list.get("items")
    m3ms = m3m_list.get("items")
    hosts = bmh_list.get("items")
    if not all(isinstance(items, list) for items in (machines, m3ms, hosts)):
        raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_INVENTORY_INVALID")

    cluster_machines = []
    machine_by_name = {}
    machine_uids = set()
    for machine in machines:
        if not isinstance(machine, dict):
            raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_INVENTORY_INVALID")
        metadata = machine.get("metadata", {})
        if not isinstance(metadata, dict):
            raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_MACHINE_METADATA_INVALID")
        labels = metadata.get("labels", {})
        if not isinstance(labels, dict):
            raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_MACHINE_LABELS_INVALID")
        if labels.get("cluster.x-k8s.io/cluster-name") != cluster_name:
            continue
        name = metadata.get("name")
        uid = metadata.get("uid")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(uid, str)
            or not uid
            or name in machine_by_name
            or uid in machine_uids
        ):
            raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_MACHINE_IDENTITY_INVALID")
        machine_by_name[name] = machine
        machine_uids.add(uid)
        cluster_machines.append(machine)
    if not cluster_machines:
        raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_NO_CLUSTER_MACHINES")

    m3m_by_name = {}
    m3m_uids = set()
    for m3m in m3ms:
        if not isinstance(m3m, dict):
            raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_INVENTORY_INVALID")
        metadata = m3m.get("metadata", {})
        if not isinstance(metadata, dict):
            raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_METAL3MACHINE_METADATA_INVALID")
        labels = metadata.get("labels", {})
        if not isinstance(labels, dict):
            raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_METAL3MACHINE_LABELS_INVALID")
        if labels.get("cluster.x-k8s.io/cluster-name") == cluster_name:
            name = metadata.get("name")
            uid = metadata.get("uid")
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(uid, str)
                or not uid
                or name in m3m_by_name
                or uid in m3m_uids
            ):
                raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_METAL3MACHINE_IDENTITY_INVALID")
            m3m_by_name[name] = m3m
            m3m_uids.add(uid)
    if not m3m_by_name:
        raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_NO_METAL3MACHINES")

    if len(m3m_by_name) != len(machine_by_name):
        raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_MACHINE_INFRASTRUCTURE_COUNT_MISMATCH")
    machine_infrastructure = {}
    infrastructure_api_versions = {}
    for machine_name in machine_by_name:
        machine_spec = machine_by_name[machine_name].get("spec")
        if not isinstance(machine_spec, dict):
            raise CapiPivotError(
                "CAPI_PIVOT_POSTFLIGHT_MACHINE_SPEC_INVALID:" + machine_name
            )
        infrastructure_ref = machine_spec.get("infrastructureRef")
        if (
            not isinstance(infrastructure_ref, dict)
            or infrastructure_ref.get("kind") != "Metal3Machine"
            or not isinstance(infrastructure_ref.get("name"), str)
            or not infrastructure_ref.get("name")
            or not str(infrastructure_ref.get("apiVersion", "")).startswith(
                "infrastructure.cluster.x-k8s.io/"
            )
        ):
            raise CapiPivotError(
                "CAPI_PIVOT_POSTFLIGHT_MACHINE_INFRASTRUCTURE_REFERENCE_INVALID:"
                + machine_name
            )
        m3m_name = infrastructure_ref["name"]
        if m3m_name not in m3m_by_name:
            raise CapiPivotError(
                "CAPI_PIVOT_POSTFLIGHT_METAL3MACHINE_NOT_FOUND:"
                + machine_name
                + ":"
                + m3m_name
            )
        machine_infrastructure[m3m_name] = machine_name
        infrastructure_api_versions[m3m_name] = infrastructure_ref["apiVersion"]
    if set(machine_infrastructure) != set(m3m_by_name):
        raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_ORPHAN_METAL3MACHINE")

    bmh_by_name = {}
    bmh_uids = set()
    for host in hosts:
        if not isinstance(host, dict):
            raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_INVENTORY_INVALID")
        metadata = host.get("metadata", {})
        if not isinstance(metadata, dict):
            raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_BMH_METADATA_INVALID")
        name = metadata.get("name")
        uid = metadata.get("uid")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(uid, str)
            or not uid
            or name in bmh_by_name
            or uid in bmh_uids
        ):
            raise CapiPivotError("CAPI_PIVOT_POSTFLIGHT_BMH_IDENTITY_INVALID")
        bmh_by_name[name] = host
        bmh_uids.add(uid)

    verified = []
    for m3m_name, m3m in sorted(m3m_by_name.items()):
        annotations = m3m.get("metadata", {}).get("annotations", {})
        host_name = (
            annotations.get("metal3.io/BareMetalHost")
            if isinstance(annotations, dict)
            else None
        )
        if not isinstance(host_name, str) or not host_name:
            raise CapiPivotError(
                "CAPI_PIVOT_POSTFLIGHT_METAL3MACHINE_BMH_REFERENCE_MISSING:" + m3m_name
            )
        host = bmh_by_name.get(host_name)
        if host is None:
            raise CapiPivotError(
                "CAPI_PIVOT_POSTFLIGHT_BMH_NOT_FOUND:" + m3m_name + ":" + host_name
            )
        m3m_uid = m3m.get("metadata", {}).get("uid")
        host_spec = host.get("spec")
        if not isinstance(host_spec, dict):
            raise CapiPivotError(
                "CAPI_PIVOT_POSTFLIGHT_BMH_SPEC_INVALID:" + host_name
            )
        consumer_ref = host_spec.get("consumerRef")
        if (
            not isinstance(consumer_ref, dict)
            or consumer_ref.get("apiVersion") != infrastructure_api_versions[m3m_name]
            or consumer_ref.get("kind") != "Metal3Machine"
            or consumer_ref.get("name") != m3m_name
            or consumer_ref.get("namespace") != namespace
            or consumer_ref.get("uid") != m3m_uid
        ):
            raise CapiPivotError(
                "CAPI_PIVOT_POSTFLIGHT_BMH_CONSUMER_REFERENCE_MISMATCH:"
                + host_name
            )
        host_meta = host.get("metadata", {})
        host_annotations = host_meta.get("annotations", {}) if isinstance(host_meta, dict) else {}
        if not isinstance(host_annotations, dict):
            raise CapiPivotError(
                "CAPI_PIVOT_POSTFLIGHT_BMH_ANNOTATIONS_INVALID:" + host_name
            )
        if "baremetalhost.metal3.io/status" in host_annotations:
            raise CapiPivotError(
                "CAPI_PIVOT_POSTFLIGHT_BMH_STATUS_RECONSTRUCTION_PENDING:" + host_name
            )
        status = host.get("status")
        if not isinstance(status, dict):
            raise CapiPivotError(
                "CAPI_PIVOT_POSTFLIGHT_BMH_STATUS_INVALID:" + host_name
            )
        provisioning = status.get("provisioning", {})
        state = provisioning.get("state") if isinstance(provisioning, dict) else None
        operational = status.get("operationalStatus")
        if state not in {"available", "ready", "provisioned", "externally provisioned"}:
            raise CapiPivotError(
                "CAPI_PIVOT_POSTFLIGHT_BMH_NOT_STABLE:" + host_name
            )
        if operational != "OK":
            raise CapiPivotError(
                "CAPI_PIVOT_POSTFLIGHT_BMH_NOT_HEALTHY:" + host_name
            )
        if status.get("errorMessage") or status.get("errorType"):
            raise CapiPivotError(
                "CAPI_PIVOT_POSTFLIGHT_BMH_HAS_ERROR:" + host_name
            )
        verified.append({
            "metal3machine": m3m_name,
            "baremetalhost": host_name,
            "provisioning_state": state,
            "operational_status": operational,
            "status_annotation_consumed": True,
        })

    return {
        "cluster_name": cluster_name,
        "cluster_uid": cluster_uid,
        "namespace": namespace,
        "baremetal_host_namespace": host_namespace,
        "postflight_passed": True,
        "execution_mode": "read_only_target_cluster_verification",
        "cluster_machine_count": len(cluster_machines),
        "metal3machine_count": len(m3m_by_name),
        "verified_bmh_count": len(verified),
        "verified_bmhs": verified[:200],
        "verified_machine_count": len(machine_by_name),
        "machine_infrastructure_references_verified": True,
        "bmh_consumer_references_verified": True,
        "controllers_ready": True,
        "controllers": controller_evidence,
        "bmh_status_annotations_consumed": True,
        "side_effects": False,
    }
