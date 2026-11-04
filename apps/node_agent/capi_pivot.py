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
    clusterctl = lookup("clusterctl")
    if not clusterctl:
        raise CapiPivotError("CLUSTERCTL_NOT_INSTALLED")

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
    process_env = {
        "PATH": env.get("PATH", ""),
        "HOME": env.get("HOME", "/tmp"),
        "KUBECONFIG": str(source_kubeconfig),
    }
    try:
        completed = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
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
        "namespace_scope": "all_capi_objects_in_namespace",
        "namespace_scope_warning": True,
        "side_effects": False,
        "exit_code": completed.returncode,
        "stdout_summary": stdout,
        "stderr_summary": stderr,
    }
