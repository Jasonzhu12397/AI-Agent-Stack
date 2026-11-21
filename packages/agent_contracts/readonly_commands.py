"""Read-only diagnostic commands both Agents answer from the same code.

``agent_commands`` authorized these actions long before either Agent implemented
them, so the terminal could dispatch ``cluster.health`` or
``k8s.resource.read`` and get ``ACTION_NOT_IMPLEMENTED`` back. They are pure
observations, so they are defined once here and executed by both Agents through
their own command runner.

Nothing in this module mutates the host or the cluster: every function below
issues ``kubectl get``/``--raw`` reads or ``ip``/version probes, and the result
carries ``side_effects=False``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

Runner = Callable[..., dict[str, Any]]

READONLY_ACTIONS: frozenset[str] = frozenset(
    {
        "node.health",
        "host.preflight",
        "cluster.health",
        "cluster.observe",
        "cluster.diagnose",
        "cluster.postflight",
        "cluster.network.inspect",
        "cluster.manifest.validate",
        "k8s.resource.read",
    }
)

ALLOWED_RESOURCES: tuple[str, ...] = (
    "baremetalhosts",
    "clusters",
    "configmaps",
    "customresourcedefinitions",
    "daemonsets",
    "deployments",
    "events",
    "machines",
    "namespaces",
    "nodes",
    "pods",
    "services",
    "statefulsets",
)

RESOURCE_PAYLOAD_KEYS = frozenset(
    {"resource", "namespace", "name", "label_selector", "all_namespaces"}
)
_DNS1123 = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
_SELECTOR = re.compile(r"^[A-Za-z0-9=!<>.,_\- ]{1,200}$")
_MAX_ITEMS = 64


class ReadonlyCommandError(ValueError):
    """A read-only command that cannot be executed as requested."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _succeeded(output: dict[str, Any]) -> dict[str, Any]:
    return {"status": "succeeded", "error_code": None, "output": output, "side_effects": False}


def _failed(code: str, detail: str = "", output: dict[str, Any] | None = None) -> dict[str, Any]:
    body = {"error": code}
    if detail:
        body["detail"] = detail
    if output:
        body.update(output)
    return {"status": "failed", "error_code": code, "output": body, "side_effects": False}


def _kubectl(
    run: Runner, arguments: list[str], *, stdin: str | None = None
) -> tuple[Any, dict[str, Any] | None]:
    if stdin is None:
        result = run(["kubectl", *arguments], timeout=30)
    else:
        result = run(["kubectl", *arguments], timeout=30, stdin=stdin)
    if not isinstance(result, dict):
        return None, _failed("AGENT_RUNNER_INVALID")
    if not result.get("available"):
        return None, _failed(
            "KUBECTL_UNAVAILABLE",
            str(result.get("error") or result.get("stderr") or "")[-300:],
        )
    if result.get("returncode") not in (0, None):
        return None, _failed(
            "KUBECTL_FAILED",
            str(result.get("stderr") or "")[-300:],
            {"arguments": arguments},
        )
    return result.get("stdout", ""), None


def _kubectl_json(run: Runner, arguments: list[str]) -> tuple[Any, dict[str, Any] | None]:
    stdout, error = _kubectl(run, arguments)
    if error is not None:
        return None, error
    try:
        return json.loads(stdout or ""), None
    except (TypeError, ValueError):
        return None, _failed("KUBECTL_OUTPUT_NOT_JSON", str(stdout or "")[:200])


def _yaml_dump(value: Any) -> str:
    import yaml

    return yaml.safe_dump(value, sort_keys=False)


def _condition_true(conditions: Any, kind: str) -> bool:
    if not isinstance(conditions, list):
        return False
    for condition in conditions:
        if (
            isinstance(condition, dict)
            and condition.get("type") == kind
            and str(condition.get("status")).lower() == "true"
        ):
            return True
    return False


def _node_summary(nodes: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not isinstance(nodes, dict) or not isinstance(nodes.get("items"), list):
        return {}, _failed("KUBECTL_OUTPUT_NOT_JSON", "node list has no items")
    total = len(nodes["items"])
    ready = 0
    not_ready: list[str] = []
    for node in nodes["items"][: _MAX_ITEMS * 4]:
        if not isinstance(node, dict):
            continue
        name = str(node.get("metadata", {}).get("name", ""))
        if _condition_true(node.get("status", {}).get("conditions"), "Ready"):
            ready += 1
        else:
            not_ready.append(name)
    return (
        {
            "total": total,
            "ready": ready,
            "not_ready": not_ready[: _MAX_ITEMS],
        },
        None,
    )


def _nodes(run: Runner) -> tuple[dict[str, Any], dict[str, Any] | None]:
    nodes, error = _kubectl_json(run, ["get", "nodes", "-o", "json"])
    if error is not None:
        return {}, error
    return _node_summary(nodes)


def _readiness(run: Runner) -> tuple[dict[str, Any], dict[str, Any] | None]:
    result = run(["kubectl", "get", "--raw", "/readyz"], timeout=20)
    if not isinstance(result, dict):
        return {}, _failed("AGENT_RUNNER_INVALID")
    if not result.get("available"):
        return {}, _failed("KUBECTL_UNAVAILABLE", str(result.get("stderr") or "")[-200:])
    body = str(result.get("stdout", "")).strip()
    healthy = result.get("returncode") in (0, None) and body.lower() in ("ok", "")
    return {"readyz": body[:200], "healthy": healthy}, None


def _pods(run: Runner) -> tuple[dict[str, Any], dict[str, Any] | None]:
    pods, error = _kubectl_json(run, ["get", "pods", "--all-namespaces", "-o", "json"])
    if error is not None:
        return {}, error
    if not isinstance(pods, dict) or not isinstance(pods.get("items"), list):
        return {}, _failed("KUBECTL_OUTPUT_NOT_JSON", "pod list has no items")
    total = len(pods["items"])
    not_ready = 0
    for pod in pods["items"][: _MAX_ITEMS * 4]:
        if not isinstance(pod, dict):
            continue
        phase = pod.get("status", {}).get("phase")
        if phase in ("Succeeded", "Failed"):
            continue
        if not _condition_true(pod.get("status", {}).get("conditions"), "Ready"):
            not_ready += 1
    return {"total": total, "not_ready": not_ready}, None


def _network_summary(run: Runner) -> tuple[dict[str, Any], dict[str, Any] | None]:
    interfaces: Any = None
    result = run(["ip", "-j", "addr", "show"], timeout=15)
    if isinstance(result, dict) and result.get("available"):
        try:
            interfaces = json.loads(result.get("stdout") or "[]")
        except (TypeError, ValueError):
            interfaces = None
    if interfaces is None:
        return {}, _failed("HOST_NETWORK_UNAVAILABLE", "ip -j addr show did not report interfaces")
    summary: list[dict[str, Any]] = []
    for interface in interfaces[: _MAX_ITEMS]:
        if not isinstance(interface, dict):
            continue
        addresses = [
            str(info.get("local"))
            for info in interface.get("addr_info", [])
            if isinstance(info, dict) and info.get("local")
        ][: _MAX_ITEMS]
        summary.append(
            {
                "name": interface.get("ifname"),
                "state": interface.get("operstate"),
                "addresses": addresses,
            }
        )
    default_routes: list[dict[str, Any]] = []
    routes = run(["ip", "-j", "route", "show"], timeout=15)
    if isinstance(routes, dict) and routes.get("available"):
        try:
            parsed = json.loads(routes.get("stdout") or "[]")
        except (TypeError, ValueError):
            parsed = []
        for route in parsed if isinstance(parsed, list) else []:
            if isinstance(route, dict) and route.get("dst") == "default":
                default_routes.append(
                    {"gateway": route.get("gateway"), "dev": route.get("dev")}
                )
    return {"interfaces": summary, "default_routes": default_routes[: _MAX_ITEMS]}, None


def _validate_resource_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ReadonlyCommandError("PAYLOAD_INVALID", "payload must be an object")
    unknown = set(payload) - RESOURCE_PAYLOAD_KEYS
    if unknown:
        raise ReadonlyCommandError(
            "PAYLOAD_INVALID", f"unsupported payload keys: {sorted(unknown)}"
        )
    resource = payload.get("resource")
    if not isinstance(resource, str) or resource not in ALLOWED_RESOURCES:
        raise ReadonlyCommandError(
            "RESOURCE_NOT_ALLOWED",
            "resource must be one of " + ", ".join(ALLOWED_RESOURCES),
        )
    namespace = payload.get("namespace")
    if namespace is not None and (
        not isinstance(namespace, str) or not _DNS1123.match(namespace)
    ):
        raise ReadonlyCommandError("PAYLOAD_INVALID", "namespace must be a DNS-1123 label")
    name = payload.get("name")
    if name is not None and (
        not isinstance(name, str) or not _DNS1123.match(name)
    ):
        raise ReadonlyCommandError("PAYLOAD_INVALID", "name must be a DNS-1123 name")
    selector = payload.get("label_selector")
    if selector is not None and (
        not isinstance(selector, str) or not _SELECTOR.match(selector)
    ):
        raise ReadonlyCommandError("PAYLOAD_INVALID", "label_selector is not a valid selector")
    all_namespaces = payload.get("all_namespaces", False)
    if not isinstance(all_namespaces, bool):
        raise ReadonlyCommandError("PAYLOAD_INVALID", "all_namespaces must be a boolean")
    if all_namespaces and namespace is not None:
        raise ReadonlyCommandError(
            "PAYLOAD_INVALID", "namespace and all_namespaces are mutually exclusive"
        )
    return {
        "resource": resource,
        "namespace": namespace,
        "name": name,
        "label_selector": selector,
        "all_namespaces": all_namespaces,
    }


def _run_resource_read(run: Runner, payload: Any) -> dict[str, Any]:
    request = _validate_resource_payload(payload)
    arguments = ["get", request["resource"]]
    if request["name"]:
        arguments.append(request["name"])
    if request["namespace"]:
        arguments.extend(["--namespace", request["namespace"]])
    if request["all_namespaces"]:
        arguments.append("--all-namespaces")
    if request["label_selector"]:
        arguments.extend(["--selector", request["label_selector"]])
    arguments.extend(["-o", "json"])
    document, error = _kubectl_json(run, arguments)
    if error is not None:
        return error
    items = document.get("items") if isinstance(document, dict) else None
    if isinstance(document, list):
        items = document
    return _succeeded(
        {
            "resource": request["resource"],
            "item_count": len(items) if isinstance(items, list) else None,
            "result": document,
        }
    )


def _run_node_health(run: Runner) -> dict[str, Any]:
    readiness, error = _readiness(run)
    if error is not None:
        return error
    summary, error = _nodes(run)
    if error is not None:
        return error
    return _succeeded({"kubelet": readiness, "nodes": summary})


def _run_host_preflight(run: Runner) -> dict[str, Any]:
    tools: dict[str, Any] = {}
    probes = {
        "kubectl": ["kubectl", "version", "--client", "-o", "json"],
        "nmstatectl": ["nmstatectl", "--version"],
        "kubeadm": ["kubeadm", "version", "-o", "json"],
    }
    for name, argv in probes.items():
        result = run(argv, timeout=15)
        available = isinstance(result, dict) and bool(result.get("available"))
        tools[name] = {
            "available": available,
            "detail": str((result or {}).get("stdout") or (result or {}).get("error") or "")[
                :200
            ],
        }
    hostname = run(["hostname"], timeout=10)
    host = str(hostname.get("stdout", "")).strip() if isinstance(hostname, dict) else ""
    return _succeeded(
        {
            "host": host[:253],
            "tools": tools,
            "ok": bool(tools["kubectl"]["available"]),
            "optional_missing": [
                name for name in ("nmstatectl", "kubeadm") if not tools[name]["available"]
            ],
        }
    )


def _run_cluster_health(run: Runner) -> dict[str, Any]:
    readiness, error = _readiness(run)
    if error is not None:
        return error
    summary, error = _nodes(run)
    if error is not None:
        return error
    healthy = readiness["healthy"] and summary["total"] > 0 and summary["ready"] == summary["total"]
    return _succeeded({"readyz": readiness, "nodes": summary, "healthy": healthy})


def _run_cluster_observe(run: Runner) -> dict[str, Any]:
    info, error = _kubectl(run, ["cluster-info"])
    if error is not None:
        return error
    summary, error = _nodes(run)
    if error is not None:
        return error
    namespaces, error = _kubectl_json(run, ["get", "namespaces", "-o", "json"])
    if error is not None:
        return error
    count = len(namespaces.get("items", [])) if isinstance(namespaces, dict) else None
    return _succeeded(
        {
            "cluster_info": str(info or "")[:2000],
            "nodes": summary,
            "namespace_count": count,
        }
    )


DIAGNOSE_PAYLOAD_KEYS = frozenset({"namespace"})


def _run_cluster_diagnose(run: Runner, payload: Any = None) -> dict[str, Any]:
    namespace = None
    if isinstance(payload, dict) and payload:
        unknown = set(payload) - DIAGNOSE_PAYLOAD_KEYS
        namespace = payload.get("namespace")
        if unknown or (namespace is not None and (
            not isinstance(namespace, str) or not _DNS1123.match(namespace)
        )):
            raise ReadonlyCommandError("PAYLOAD_INVALID", "diagnose payload is invalid")

    nodes, error = _kubectl_json(run, ["get", "nodes", "-o", "json"])
    if error is not None:
        return error
    summary, error = _node_summary(nodes)
    if error is not None:
        return error
    pressure: list[dict[str, Any]] = []
    for node in nodes.get("items", [])[: _MAX_ITEMS * 4]:
        if not isinstance(node, dict):
            continue
        name = str(node.get("metadata", {}).get("name", ""))
        for condition in node.get("status", {}).get("conditions", []) or []:
            if (
                isinstance(condition, dict)
                and condition.get("type") in ("MemoryPressure", "DiskPressure", "PIDPressure")
                and str(condition.get("status")).lower() == "true"
            ):
                pressure.append({"node": name, "condition": condition.get("type")})
    event_arguments = ["get", "events"]
    if namespace is None:
        event_arguments.append("--all-namespaces")
    else:
        event_arguments.extend(["--namespace", namespace])
    event_arguments.extend(["--field-selector", "type=Warning", "-o", "json"])
    events, error = _kubectl_json(run, event_arguments)
    warnings: list[dict[str, Any]] = []
    if error is None and isinstance(events, dict):
        for event in events.get("items", [])[: _MAX_ITEMS]:
            if not isinstance(event, dict):
                continue
            warnings.append(
                {
                    "namespace": event.get("metadata", {}).get("namespace"),
                    "reason": event.get("reason"),
                    "object": event.get("involvedObject", {}).get("name"),
                    "message": str(event.get("message", ""))[:200],
                }
            )
    return _succeeded(
        {
            "nodes": summary,
            "node_pressure": pressure[: _MAX_ITEMS],
            "warning_events": warnings,
            "events_available": error is None,
        }
    )


def _run_cluster_postflight(run: Runner) -> dict[str, Any]:
    summary, error = _nodes(run)
    if error is not None:
        return error
    pod_summary, error = _pods(run)
    if error is not None:
        return error
    ok = (
        summary["total"] > 0
        and summary["ready"] == summary["total"]
        and pod_summary["not_ready"] == 0
    )
    return _succeeded({"nodes": summary, "pods": pod_summary, "ok": ok})


MANIFEST_PAYLOAD_KEYS = frozenset({"manifests", "namespace"})


def _run_manifest_validate(run: Runner, payload: Any) -> dict[str, Any]:
    """Server-side dry run of operator-supplied manifests.

    ``kubectl apply --dry-run=server`` asks the API server to validate the
    objects, so no object is created: the command stays read-only while still
    catching schema and admission problems that a client-side check misses.
    """

    if not isinstance(payload, dict):
        raise ReadonlyCommandError("PAYLOAD_INVALID", "payload must be an object")
    unknown = set(payload) - MANIFEST_PAYLOAD_KEYS
    if unknown:
        raise ReadonlyCommandError(
            "PAYLOAD_INVALID", f"unsupported payload keys: {sorted(unknown)}"
        )
    manifests = payload.get("manifests")
    if isinstance(manifests, str):
        manifests = [manifests]
    if not isinstance(manifests, list) or not manifests or len(manifests) > 32:
        raise ReadonlyCommandError(
            "PAYLOAD_INVALID", "manifests must be a non-empty list of up to 32 documents"
        )
    documents = []
    for item in manifests:
        if not isinstance(item, (str, dict)):
            raise ReadonlyCommandError("PAYLOAD_INVALID", "each manifest must be text or an object")
        documents.append(item if isinstance(item, str) else _yaml_dump(item))
    namespace = payload.get("namespace")
    if namespace is not None and (
        not isinstance(namespace, str) or not _DNS1123.match(namespace)
    ):
        raise ReadonlyCommandError("PAYLOAD_INVALID", "namespace must be a DNS-1123 label")
    body = "\n---\n".join(documents)
    arguments = ["apply", "--dry-run=server", "-f", "-"]
    if namespace:
        arguments.extend(["--namespace", namespace])
    stdout, error = _kubectl(run, arguments, stdin=body)
    if error is not None:
        return error
    lines = [line for line in str(stdout or "").splitlines() if line.strip()]
    return _succeeded(
        {
            "validated": True,
            "document_count": len(documents),
            "results": lines[: _MAX_ITEMS],
        }
    )


def _run_network_inspect(run: Runner) -> dict[str, Any]:
    summary, error = _network_summary(run)
    if error is not None:
        return error
    return _succeeded(summary)


_DISPATCH: dict[str, Callable[..., dict[str, Any]]] = {
    "node.health": _run_node_health,
    "host.preflight": _run_host_preflight,
    "cluster.health": _run_cluster_health,
    "cluster.observe": _run_cluster_observe,
    "cluster.diagnose": _run_cluster_diagnose,
    "cluster.postflight": _run_cluster_postflight,
    "cluster.network.inspect": _run_network_inspect,
    "cluster.manifest.validate": _run_manifest_validate,
    "k8s.resource.read": _run_resource_read,
}


def execute_readonly(action: str, payload: Any, run: Runner) -> dict[str, Any]:
    """Execute one read-only diagnostic command.

    ``run(argv, timeout=...)`` returns the Agent's read-only runner envelope
    (``available``/``returncode``/``stdout``/``stderr``). The result is the
    standard command outcome, so both Agents return it unchanged.
    """

    handler = _DISPATCH.get(action)
    if handler is None:
        raise ReadonlyCommandError("ACTION_NOT_READONLY", f"action {action!r} is not read-only")
    try:
        if action in ("k8s.resource.read", "cluster.manifest.validate"):
            return handler(run, payload)
        if action == "cluster.diagnose":
            return handler(run, payload)
        if isinstance(payload, dict) and payload:
            raise ReadonlyCommandError(
                "PAYLOAD_INVALID", f"{action} does not accept payload keys"
            )
        return handler(run)
    except ReadonlyCommandError as exc:
        return {
            "status": "blocked",
            "error_code": exc.code,
            "output": {"detail": exc.detail} if exc.detail else {},
            "side_effects": False,
        }
