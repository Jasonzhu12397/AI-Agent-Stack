"""Runner-backed adapter for the bootstrap cluster control stack.

The control-stack planner in :mod:`apps.eph_agent.control_stack` only declares
which controllers must be installed and in which order. This adapter applies
that plan through an injected Kubernetes runner and observes each controller
Deployment becoming ready.

The runner contract matches the rest of the EPH Agent: a callable taking an
argument list and keyword timeouts, returning ``{"available", "returncode",
"stdout", "stderr"}``. No subprocess, manifest store or cluster access happens
inside this module; the caller injects the runner so the adapter stays testable
offline with mocks.

Manifest provisioning is data driven: the caller may supply a ``manifests``
mapping keyed by component name. When a controller is missing and no manifest
is supplied for it, the adapter records ``apply_pending`` instead of inventing
a hard-coded install source.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from .control_stack import StackComponent
from .control_stack_manifests import MAX_MANIFEST_BYTES
from .metal3_skill import Metal3SkillError

FIELD_MANAGER = "ai-agent-eph-agent"
DEFAULT_TIMEOUT_SECONDS = 1800


class ControlStackAdapterError(Metal3SkillError):
    """Raised when a control-stack component cannot be observed or applied."""


def _invoke(
    runner: Callable[..., dict[str, Any]],
    arguments: list[str],
    timeout: int,
) -> dict[str, Any]:
    result = runner(arguments, timeout=timeout, max_output_bytes=MAX_MANIFEST_BYTES)
    if not isinstance(result, dict) or result.get("available") is not True:
        raise ControlStackAdapterError("CONTROL_STACK_API_UNAVAILABLE")
    if result.get("returncode") != 0:
        raise ControlStackAdapterError("CONTROL_STACK_OPERATION_FAILED")
    return result


def _deployment_state(document: dict[str, Any]) -> dict[str, Any]:
    metadata = document.get("metadata")
    spec = document.get("spec")
    status = document.get("status")
    if not isinstance(metadata, dict) or not isinstance(spec, dict) or not isinstance(status, dict):
        raise ControlStackAdapterError("CONTROL_STACK_DEPLOYMENT_DOCUMENT_INVALID")
    desired = spec.get("replicas")
    if not isinstance(desired, int) or isinstance(desired, bool) or desired < 0:
        desired = 1
    available = status.get("availableReplicas")
    ready = status.get("readyReplicas")
    if not isinstance(available, int) or isinstance(available, bool):
        available = 0
    if not isinstance(ready, int) or isinstance(ready, bool):
        ready = 0
    return {
        "desired_replicas": desired,
        "available_replicas": available,
        "ready_replicas": ready,
        "ready": ready >= desired and available >= desired,
    }


def _ready_conditions(document: dict[str, Any]) -> list[dict[str, Any]]:
    status = document.get("status")
    if not isinstance(status, dict):
        return []
    conditions = status.get("conditions")
    if not isinstance(conditions, list):
        return []
    return [item for item in conditions if isinstance(item, dict)]


def _observe_custom_resource(
    probe: dict[str, str],
    runner: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Observe the workload CR behind an operator Deployment."""

    observation = {
        "api_version": probe["api_version"],
        "kind": probe["kind"],
        "name": probe["name"],
        "namespace": probe["namespace"],
        "ready": False,
        "reason": "NOT_FOUND",
    }
    result = runner(
        [
            "get",
            probe["kind"].lower(),
            probe["name"],
            "--namespace",
            probe["namespace"],
            "-o",
            "json",
        ],
        timeout=30,
        max_output_bytes=MAX_MANIFEST_BYTES,
    )
    if not isinstance(result, dict):
        return {**observation, "reason": "API_UNAVAILABLE"}
    if result.get("returncode") != 0:
        error = str(result.get("stderr", "")).lower()
        if "notfound" in error or "not found" in error:
            return observation
        return {**observation, "reason": "API_UNAVAILABLE"}
    try:
        document = yaml.safe_load(result.get("stdout", ""))
    except (TypeError, ValueError, yaml.YAMLError):
        return {**observation, "reason": "DOCUMENT_INVALID"}
    if not isinstance(document, dict):
        return {**observation, "reason": "DOCUMENT_INVALID"}
    conditions = _ready_conditions(document)
    ready = any(
        str(condition.get("type")) == "Ready" and str(condition.get("status")) == "True"
        for condition in conditions
    )
    return {
        **observation,
        "ready": ready,
        "reason": None if ready else "CONDITION_NOT_READY",
        "conditions": conditions[:8],
    }


def observe_component(
    component: StackComponent,
    runner: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Observe one controller Deployment and return its replica readiness."""

    result = _invoke(
        runner,
        [
            "get",
            "deployment",
            component.controller,
            "--namespace",
            component.namespace,
            "-o",
            "json",
        ],
        60,
    )
    try:
        document = yaml.safe_load(result.get("stdout", ""))
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        raise ControlStackAdapterError("CONTROL_STACK_DEPLOYMENT_JSON_INVALID") from exc
    if not isinstance(document, dict):
        raise ControlStackAdapterError("CONTROL_STACK_DEPLOYMENT_JSON_INVALID")
    state = _deployment_state(document)
    custom_resource = None
    probe = component.custom_resource_probe()
    if probe is not None:
        # A ready operator must also have reconciled its workload, otherwise the
        # service the rest of the bootstrap depends on is not actually up.
        custom_resource = _observe_custom_resource(probe, runner)
        state["ready"] = state["ready"] and custom_resource["ready"]
    return {
        "name": component.name,
        "controller": component.controller,
        "namespace": component.namespace,
        "install_order": component.install_order,
        "version": component.version,
        **state,
        "custom_resource": custom_resource,
        "side_effects": False,
    }


def _existing_component(
    component: StackComponent,
    runner: Callable[..., dict[str, Any]],
) -> dict[str, Any] | None:
    result = runner(
        [
            "get",
            "deployment",
            component.controller,
            "--namespace",
            component.namespace,
            "-o",
            "json",
        ],
        timeout=30,
        max_output_bytes=MAX_MANIFEST_BYTES,
    )
    if not isinstance(result, dict):
        return None
    if result.get("available") is False:
        error = str(result.get("stderr", "")).lower()
        if "notfound" in error or "not found" in error:
            return None
        raise ControlStackAdapterError("CONTROL_STACK_API_UNAVAILABLE")
    if result.get("returncode") != 0:
        return None
    try:
        document = yaml.safe_load(result.get("stdout", ""))
    except (TypeError, ValueError, yaml.YAMLError):
        return None
    return document if isinstance(document, dict) else None


def _apply_manifest(
    manifest: dict[str, Any] | list[dict[str, Any]],
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any,
) -> None:
    if isinstance(manifest, list):
        serialized = yaml.safe_dump_all(manifest, sort_keys=False)
    else:
        serialized = yaml.safe_dump(manifest, sort_keys=False)
    encoded = serialized.encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ControlStackAdapterError("CONTROL_STACK_MANIFEST_TOO_LARGE")
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".yaml",
            encoding="utf-8",
            delete=False,
        ) as handle:
            handle.write(serialized)
            path = Path(handle.name)
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
            120,
        )
        if lease_lost_event is not None and lease_lost_event.is_set():
            raise ControlStackAdapterError("COMMAND_LEASE_LOST_AFTER_DRY_RUN")
        try:
            _invoke(
                runner,
                [
                    "apply",
                    "--server-side",
                    f"--field-manager={FIELD_MANAGER}",
                    "-f",
                    str(path),
                ],
                300,
            )
        except ControlStackAdapterError as exc:
            exc.side_effects = True
            raise
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def apply_workload_resource(
    document: dict[str, Any],
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any = None,
    crd_name: str | None = None,
) -> None:
    """Apply a workload CR after waiting for its CRD to be served.

    A CR cannot share one server-side apply with the CRD that introduces it:
    the API discovery used for the batch happens before the CRD is persisted.
    The control stack therefore applies the operator first, waits here for
    ``established`` and only then applies the custom resource.
    """

    if not isinstance(document, dict):
        raise ControlStackAdapterError("CONTROL_STACK_WORKLOAD_DOCUMENT_INVALID")
    if lease_lost_event is not None and lease_lost_event.is_set():
        raise ControlStackAdapterError("COMMAND_LEASE_LOST_BEFORE_CONTROL_STACK_APPLY")
    if crd_name:
        try:
            _invoke(
                runner,
                [
                    "wait",
                    "--for",
                    "condition=established",
                    "--timeout=120s",
                    f"crd/{crd_name}",
                ],
                150,
            )
        except ControlStackAdapterError as exc:
            raise ControlStackAdapterError(
                "CONTROL_STACK_CRD_NOT_ESTABLISHED"
            ) from exc
    _apply_manifest(document, runner, lease_lost_event)


def apply_control_stack(
    plan: list[StackComponent],
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any = None,
    manifests: dict[str, dict[str, Any] | list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Apply the control-stack plan idempotently through the injected runner.

    Existing controllers are left untouched. A missing controller is applied
    only when a manifest is supplied for it; otherwise it is recorded as
    ``apply_pending`` so the caller can supply a manifest source later.
    """

    if lease_lost_event is not None and lease_lost_event.is_set():
        raise ControlStackAdapterError("COMMAND_LEASE_LOST_BEFORE_CONTROL_STACK_APPLY")
    if not isinstance(plan, list) or not plan:
        raise ControlStackAdapterError("CONTROL_STACK_PLAN_INVALID")
    orders = sorted(component.install_order for component in plan)
    if orders != list(range(1, len(plan) + 1)):
        raise ControlStackAdapterError("CONTROL_STACK_PLAN_ORDER_INVALID")
    ordered = sorted(plan, key=lambda component: component.install_order)

    manifest_source = manifests if isinstance(manifests, dict) else {}
    applied: list[dict[str, Any]] = []
    pending: list[str] = []
    already: list[str] = []

    for component in ordered:
        existing = _existing_component(component, runner)
        if existing is not None:
            already.append(component.name)
            continue
        manifest = manifest_source.get(component.name)
        if manifest is None:
            pending.append(component.name)
            continue
        if lease_lost_event is not None and lease_lost_event.is_set():
            raise ControlStackAdapterError("COMMAND_LEASE_LOST_DURING_CONTROL_STACK_APPLY")
        _apply_manifest(manifest, runner, lease_lost_event)
        applied.append(component.name)

    return {
        "component_count": len(ordered),
        "applied_components": applied,
        "already_present_components": already,
        "apply_pending_components": pending,
        "install_order": [component.name for component in ordered],
        "side_effects": bool(applied),
    }


def wait_for_control_stack_ready(
    plan: list[StackComponent],
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Poll every planned controller until its Deployment is ready."""

    if not isinstance(plan, list) or not plan:
        raise ControlStackAdapterError("CONTROL_STACK_PLAN_INVALID")
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or not 60 <= timeout_seconds <= 3600
    ):
        raise ControlStackAdapterError("CONTROL_STACK_READY_TIMEOUT_INVALID")
    deadline = monotonic() + timeout_seconds
    delay = 10.0
    latest: dict[str, Any] = {}
    pending = {component.name for component in plan}
    by_name = {component.name: component for component in plan}

    while pending and monotonic() < deadline:
        if lease_lost_event is not None and lease_lost_event.is_set():
            raise ControlStackAdapterError("COMMAND_LEASE_LOST_DURING_CONTROL_STACK_WAIT")
        for name in tuple(pending):
            observed = observe_component(by_name[name], runner)
            latest[name] = observed
            if observed["ready"]:
                pending.remove(name)
        if pending:
            remaining = max(0.0, deadline - monotonic())
            sleep(min(delay, remaining))
            delay = min(delay * 1.5, 30.0)

    if pending:
        raise ControlStackAdapterError(
            "CONTROL_STACK_READY_TIMEOUT:" + ",".join(sorted(pending))
        )
    return {
        "ready_components": sorted(latest),
        "ready_component_count": len(latest),
        "components": [latest[name] for name in sorted(latest)],
        "side_effects": False,
    }
