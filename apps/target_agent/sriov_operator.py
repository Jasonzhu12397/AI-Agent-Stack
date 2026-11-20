"""Install the SR-IOV Network Operator before applying node policies.

The target cluster starts without SR-IOV CRDs. A ``SriovNetworkNodePolicy``
rendered by the platform can only be applied after the operator CRDs and the
operator Deployment are ready. This module keeps that ordering explicit and
uses the vendored offline release manifest when the caller does not supply one.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

FIELD_MANAGER = "ai-agent-target-sriov-operator"
DEFAULT_MANIFEST_DIR = Path("/opt/addons/sriov-network-operator")
REPO_MANIFEST_DIR = (
    Path(__file__).resolve().parents[2] / "deploy/offline/addons/sriov-network-operator"
)
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
OPERATOR_NAMESPACE = "sriov-network-operator"
OPERATOR_DEPLOYMENT = "sriov-network-operator"
POLICY_CRD = "sriovnetworknodepolicies.sriovnetwork.openshift.io"


class SriovOperatorError(ValueError):
    def __init__(self, code: str, side_effects: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.side_effects = side_effects


def _manifest_directory(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("SRIOV_OPERATOR_MANIFEST_DIR")
    if configured:
        return Path(configured)
    if DEFAULT_MANIFEST_DIR.is_dir():
        return DEFAULT_MANIFEST_DIR
    return REPO_MANIFEST_DIR


def _load_manifest(
    supplied: Any = None,
    *,
    environ: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    if supplied is not None:
        if isinstance(supplied, dict):
            docs = [supplied]
        elif isinstance(supplied, list) and all(isinstance(item, dict) for item in supplied):
            docs = supplied
        elif isinstance(supplied, str):
            try:
                docs = [
                    item
                    for item in yaml.safe_load_all(supplied)
                    if isinstance(item, dict) and item
                ]
            except (ValueError, yaml.YAMLError) as exc:
                raise SriovOperatorError("SRIOV_OPERATOR_MANIFEST_INVALID") from exc
        else:
            raise SriovOperatorError("SRIOV_OPERATOR_MANIFEST_INVALID")
    else:
        path = _manifest_directory(environ) / "operator.yaml"
        if not path.is_file():
            raise SriovOperatorError("SRIOV_OPERATOR_MANIFEST_MISSING")
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise SriovOperatorError("SRIOV_OPERATOR_MANIFEST_TOO_LARGE")
        try:
            docs = [
                item
                for item in yaml.safe_load_all(path.read_text(encoding="utf-8"))
                if isinstance(item, dict) and item
            ]
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise SriovOperatorError("SRIOV_OPERATOR_MANIFEST_INVALID") from exc
    if not docs:
        raise SriovOperatorError("SRIOV_OPERATOR_MANIFEST_EMPTY")
    return docs


def _invoke(
    runner: Callable[..., dict[str, Any]],
    arguments: list[str],
    timeout: int,
) -> dict[str, Any]:
    result = runner(arguments, timeout=timeout, max_output_bytes=MAX_MANIFEST_BYTES)
    if not isinstance(result, dict) or result.get("available") is not True:
        raise SriovOperatorError("SRIOV_OPERATOR_API_UNAVAILABLE")
    if result.get("returncode") != 0:
        raise SriovOperatorError("SRIOV_OPERATOR_OPERATION_FAILED")
    return result


def _apply_documents(
    documents: list[dict[str, Any]],
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any,
) -> None:
    serialized = yaml.safe_dump_all(documents, sort_keys=False)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix="sriov-", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(serialized)
        path = Path(handle.name)
    try:
        os.chmod(path, 0o600)
        args = [
            "apply",
            "--server-side",
            f"--field-manager={FIELD_MANAGER}",
            "-f",
            str(path),
        ]
        if lease_lost_event is not None and lease_lost_event.is_set():
            raise SriovOperatorError("SRIOV_OPERATOR_LEASE_LOST", True)
        _invoke(runner, args, 180)
    finally:
        path.unlink(missing_ok=True)


def _wait_crd(runner: Callable[..., dict[str, Any]]) -> None:
    _invoke(
        runner,
        [
            "wait",
            f"crd/{POLICY_CRD}",
            "--for=condition=Established",
            "--timeout=180s",
        ],
        200,
    )


def _deployment_ready(runner: Callable[..., dict[str, Any]]) -> bool:
    result = _invoke(
        runner,
        [
            "get",
            "deployment",
            OPERATOR_DEPLOYMENT,
            "--namespace",
            OPERATOR_NAMESPACE,
            "-o",
            "json",
        ],
        60,
    )
    try:
        document = json.loads(result.get("stdout") or "")
    except (TypeError, ValueError) as exc:
        raise SriovOperatorError("SRIOV_OPERATOR_DEPLOYMENT_JSON_INVALID") from exc
    status = document.get("status") if isinstance(document, dict) else None
    if not isinstance(status, dict):
        return False
    ready = status.get("readyReplicas", 0)
    desired = document.get("spec", {}).get("replicas", 1)
    return (
        isinstance(ready, int)
        and not isinstance(ready, bool)
        and isinstance(desired, int)
        and ready >= desired
        and desired > 0
    )


def _wait_operator(
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any,
    *,
    timeout_seconds: int,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> None:
    deadline = monotonic() + timeout_seconds
    attempts = 0
    max_attempts = max(2, timeout_seconds // 5 + 2)
    delay = 5.0
    while monotonic() < deadline and attempts < max_attempts:
        attempts += 1
        if lease_lost_event is not None and lease_lost_event.is_set():
            raise SriovOperatorError("SRIOV_OPERATOR_LEASE_LOST", True)
        if _deployment_ready(runner):
            return
        sleep(delay)
        delay = min(delay * 1.5, 30.0)
    raise SriovOperatorError("SRIOV_OPERATOR_READY_TIMEOUT", True)


def _policies(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 64:
        raise SriovOperatorError("SRIOV_POLICIES_INVALID")
    result = []
    for item in value:
        if (
            not isinstance(item, dict)
            or item.get("kind") != "SriovNetworkNodePolicy"
            or not isinstance(item.get("metadata"), dict)
            or not isinstance(item.get("metadata", {}).get("name"), str)
        ):
            raise SriovOperatorError("SRIOV_POLICIES_INVALID")
        result.append(item)
    return result


def execute_sriov_operator(
    policies_value: Any,
    *,
    runner: Callable[..., dict[str, Any]],
    manifest: Any = None,
    lease_lost_event: Any = None,
    environ: dict[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    """Install the operator when policies exist, then apply those policies."""

    policies = _policies(policies_value)
    if not policies:
        return {
            "sriov_operator_installed": False,
            "sriov_operator_ready": False,
            "sriov_policies_expected": False,
            "sriov_policies_applied": [],
            "side_effects": False,
        }
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or not 60 <= timeout_seconds <= 1800
    ):
        raise SriovOperatorError("SRIOV_OPERATOR_TIMEOUT_INVALID")
    docs = _load_manifest(manifest, environ=environ)
    crds = [doc for doc in docs if doc.get("kind") == "CustomResourceDefinition"]
    rest = [doc for doc in docs if doc.get("kind") != "CustomResourceDefinition"]
    if not crds:
        raise SriovOperatorError("SRIOV_OPERATOR_CRDS_MISSING")
    _apply_documents(crds, runner, lease_lost_event)
    _wait_crd(runner)
    _apply_documents(rest, runner, lease_lost_event)
    _wait_operator(
        runner,
        lease_lost_event,
        timeout_seconds=timeout_seconds,
        sleep=sleep,
        monotonic=monotonic,
    )
    names = []
    for policy in policies:
        _apply_documents([policy], runner, lease_lost_event)
        names.append(policy["metadata"]["name"])
    return {
        "sriov_operator_installed": True,
        "sriov_operator_ready": True,
        "sriov_operator_namespace": OPERATOR_NAMESPACE,
        "sriov_policies_expected": True,
        "sriov_policies_applied": names,
        "side_effects": True,
    }
