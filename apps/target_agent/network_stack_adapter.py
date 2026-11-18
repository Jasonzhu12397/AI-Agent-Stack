"""Runner-backed adapter for the target cluster network stack.

The network planner in :mod:`apps.target_agent.network_plugins` declares which
CNI, multus, switch, SR-IOV and DPDK plugins belong to the target cluster. This
adapter applies that plan through an injected Kubernetes runner and observes the
plugin workloads becoming ready.

Traffic split is enforced at plan time: control-plane and management traffic
stay on the ordinary NIC, while the data-plane plugins (``ovs``, ``sriov``,
``dpdk``) carry Pod data traffic over a VF. The adapter only verifies workload
readiness; it never reconfigures host NICs or moves traffic between planes.

The runner contract matches the Target Agent: a callable taking an argument list
and keyword timeouts, returning ``{"available", "returncode", "stdout",
"stderr"}``. Workloads may be Deployments or DaemonSets; the kind is read from
``config.resource_kind`` and defaults to Deployment.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from .network_plugins import DATA_PLANE_PLUGINS, NetworkPlugin

MAX_MANIFEST_BYTES = 2 * 1024 * 1024
FIELD_MANAGER = "ai-agent-target-agent"
DEFAULT_TIMEOUT_SECONDS = 1800
WORKLOAD_KINDS = {"Deployment", "DaemonSet"}


class NetworkStackAdapterError(ValueError):
    def __init__(self, code: str, side_effects: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.side_effects = side_effects


def _invoke(
    runner: Callable[..., dict[str, Any]],
    arguments: list[str],
    timeout: int,
) -> dict[str, Any]:
    result = runner(arguments, timeout=timeout, max_output_bytes=MAX_MANIFEST_BYTES)
    if not isinstance(result, dict) or result.get("available") is not True:
        raise NetworkStackAdapterError("NETWORK_STACK_API_UNAVAILABLE")
    if result.get("returncode") != 0:
        raise NetworkStackAdapterError("NETWORK_STACK_OPERATION_FAILED")
    return result


def _workload_kind(plugin: NetworkPlugin) -> str:
    config = plugin.config if isinstance(plugin.config, dict) else {}
    kind = config.get("resource_kind", "Deployment")
    if kind not in WORKLOAD_KINDS:
        raise NetworkStackAdapterError("NETWORK_STACK_RESOURCE_KIND_INVALID")
    return kind


def _workload_state(kind: str, document: dict[str, Any]) -> dict[str, Any]:
    spec = document.get("spec")
    status = document.get("status")
    if not isinstance(spec, dict) or not isinstance(status, dict):
        raise NetworkStackAdapterError("NETWORK_STACK_WORKLOAD_DOCUMENT_INVALID")
    if kind == "DaemonSet":
        desired = status.get("desiredNumberScheduled")
        ready = status.get("numberReady")
        available = status.get("numberAvailable")
    else:
        desired = spec.get("replicas")
        available = status.get("availableReplicas")
        ready = status.get("readyReplicas")
    if not isinstance(desired, int) or isinstance(desired, bool) or desired < 0:
        desired = 1
    if not isinstance(available, int) or isinstance(available, bool):
        available = 0
    if not isinstance(ready, int) or isinstance(ready, bool):
        ready = 0
    return {
        "desired": desired,
        "available": available,
        "ready_replicas": ready,
        "ready": ready >= desired and available >= desired,
    }


def observe_plugin(
    plugin: NetworkPlugin,
    namespace: str,
    runner: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Observe one enabled plugin workload and return its readiness state."""

    kind = _workload_kind(plugin)
    resource = plugin.config.get("resource_name") if isinstance(plugin.config, dict) else None
    if not isinstance(resource, str) or not resource:
        raise NetworkStackAdapterError("NETWORK_STACK_RESOURCE_NAME_INVALID")
    result = _invoke(
        runner,
        [
            "get",
            kind.lower(),
            resource,
            "--namespace",
            namespace,
            "-o",
            "json",
        ],
        60,
    )
    try:
        document = yaml.safe_load(result.get("stdout", ""))
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        raise NetworkStackAdapterError("NETWORK_STACK_WORKLOAD_JSON_INVALID") from exc
    if not isinstance(document, dict):
        raise NetworkStackAdapterError("NETWORK_STACK_WORKLOAD_JSON_INVALID")
    state = _workload_state(kind, document)
    return {
        "name": plugin.name,
        "role": plugin.role,
        "kind": kind,
        "resource": resource,
        "version": plugin.version,
        "data_plane": plugin.name in DATA_PLANE_PLUGINS,
        **state,
        "side_effects": False,
    }


def _existing_workload(
    plugin: NetworkPlugin,
    namespace: str,
    runner: Callable[..., dict[str, Any]],
) -> dict[str, Any] | None:
    kind = _workload_kind(plugin)
    resource = plugin.config.get("resource_name") if isinstance(plugin.config, dict) else None
    if not isinstance(resource, str) or not resource:
        raise NetworkStackAdapterError("NETWORK_STACK_RESOURCE_NAME_INVALID")
    result = runner(
        [
            "get",
            kind.lower(),
            resource,
            "--namespace",
            namespace,
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
        raise NetworkStackAdapterError("NETWORK_STACK_API_UNAVAILABLE")
    if result.get("returncode") != 0:
        return None
    try:
        document = yaml.safe_load(result.get("stdout", ""))
    except (TypeError, ValueError, yaml.YAMLError):
        return None
    return document if isinstance(document, dict) else None


def _apply_manifest(
    manifest: dict[str, Any],
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any,
) -> None:
    serialized = yaml.safe_dump(manifest, sort_keys=False)
    encoded = serialized.encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise NetworkStackAdapterError("NETWORK_STACK_MANIFEST_TOO_LARGE")
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
            raise NetworkStackAdapterError("COMMAND_LEASE_LOST_AFTER_DRY_RUN")
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
        except NetworkStackAdapterError as exc:
            exc.side_effects = True
            raise
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def apply_network_stack(
    plan: list[NetworkPlugin],
    namespace: str,
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any = None,
    manifests: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply enabled network plugins idempotently through the injected runner."""

    if lease_lost_event is not None and lease_lost_event.is_set():
        raise NetworkStackAdapterError("COMMAND_LEASE_LOST_BEFORE_NETWORK_STACK_APPLY")
    if not isinstance(plan, list) or not plan:
        raise NetworkStackAdapterError("NETWORK_STACK_PLAN_INVALID")
    if not isinstance(namespace, str) or not namespace:
        raise NetworkStackAdapterError("NETWORK_STACK_NAMESPACE_INVALID")

    enabled = [plugin for plugin in plan if plugin.enabled]
    if not enabled:
        raise NetworkStackAdapterError("NETWORK_STACK_NO_ENABLED_PLUGIN")

    manifest_source = manifests if isinstance(manifests, dict) else {}
    applied: list[str] = []
    already: list[str] = []
    pending: list[str] = []
    data_plane: list[str] = []

    for plugin in enabled:
        if plugin.name in DATA_PLANE_PLUGINS:
            data_plane.append(plugin.name)
        existing = _existing_workload(plugin, namespace, runner)
        if existing is not None:
            already.append(plugin.name)
            continue
        manifest = manifest_source.get(plugin.name)
        if manifest is None:
            pending.append(plugin.name)
            continue
        if lease_lost_event is not None and lease_lost_event.is_set():
            raise NetworkStackAdapterError("COMMAND_LEASE_LOST_DURING_NETWORK_STACK_APPLY")
        _apply_manifest(manifest, runner, lease_lost_event)
        applied.append(plugin.name)

    return {
        "namespace": namespace,
        "enabled_plugins": [plugin.name for plugin in enabled],
        "data_plane_plugins": sorted(data_plane),
        "applied_plugins": applied,
        "already_present_plugins": already,
        "apply_pending_plugins": pending,
        "side_effects": bool(applied),
    }


def wait_for_network_stack_ready(
    plan: list[NetworkPlugin],
    namespace: str,
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Poll every enabled plugin workload until it is ready."""

    if not isinstance(plan, list) or not plan:
        raise NetworkStackAdapterError("NETWORK_STACK_PLAN_INVALID")
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or not 60 <= timeout_seconds <= 3600
    ):
        raise NetworkStackAdapterError("NETWORK_STACK_READY_TIMEOUT_INVALID")
    enabled = [plugin for plugin in plan if plugin.enabled]
    deadline = monotonic() + timeout_seconds
    delay = 10.0
    latest: dict[str, Any] = {}
    pending = {plugin.name for plugin in enabled}
    by_name = {plugin.name: plugin for plugin in enabled}

    while pending and monotonic() < deadline:
        if lease_lost_event is not None and lease_lost_event.is_set():
            raise NetworkStackAdapterError("COMMAND_LEASE_LOST_DURING_NETWORK_STACK_WAIT")
        for name in tuple(pending):
            observed = observe_plugin(by_name[name], namespace, runner)
            latest[name] = observed
            if observed["ready"]:
                pending.remove(name)
        if pending:
            remaining = max(0.0, deadline - monotonic())
            sleep(min(delay, remaining))
            delay = min(delay * 1.5, 30.0)

    if pending:
        raise NetworkStackAdapterError(
            "NETWORK_STACK_READY_TIMEOUT:" + ",".join(sorted(pending))
        )
    return {
        "ready_plugins": sorted(latest),
        "ready_plugin_count": len(latest),
        "data_plane_plugins": sorted(
            name for name in latest if name in DATA_PLANE_PLUGINS
        ),
        "components": [latest[name] for name in sorted(latest)],
        "side_effects": False,
    }
