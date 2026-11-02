import argparse
import hashlib
import json
import os
import platform
import re
import shutil
from pathlib import Path
import socket
import ssl
import time
import urllib.error
import urllib.request
from itertools import count
from typing import Any


class NodeAgentError(Exception):
    pass


def sanitize_kubernetes_item(resource: str, item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata", {})
    result = {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
    }
    if resource == "nodes":
        result["conditions"] = [
            {"type": condition.get("type"), "status": condition.get("status")}
            for condition in item.get("status", {}).get("conditions", [])
        ]
        result["capacity"] = item.get("status", {}).get("capacity", {})
        result["allocatable"] = item.get("status", {}).get("allocatable", {})
    elif resource == "namespaces":
        result["phase"] = item.get("status", {}).get("phase")
    elif resource == "pods":
        result["phase"] = item.get("status", {}).get("phase")
        result["node_name"] = item.get("spec", {}).get("nodeName")
    elif resource == "events":
        message = str(item.get("message", ""))[:500]
        result["type"] = item.get("type")
        result["reason"] = item.get("reason")
        result["message"] = re.sub(
            r"(?i)(password|token|secret|authorization|api[_-]?key)\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
            message,
        )
        result["involved_object"] = {
            "kind": item.get("involvedObject", {}).get("kind"),
            "name": item.get("involvedObject", {}).get("name"),
        }
    elif resource == "services":
        result["type"] = item.get("spec", {}).get("type")
        result["cluster_ip"] = item.get("spec", {}).get("clusterIP")
        result["ports"] = [
            {"port": port.get("port"), "protocol": port.get("protocol")}
            for port in item.get("spec", {}).get("ports", [])
        ]
    return result


def inspect_api_group(
    group_name: str,
    resource_names: list[str],
    namespace: str,
) -> dict[str, Any]:
    if not isinstance(namespace, str) or not namespace or "/" in namespace or ".." in namespace:
        raise NodeAgentError("INVALID_NAMESPACE")
    discovery = kubernetes_api_get("/apis")
    group = next(
        (item for item in discovery.get("groups", []) if item.get("name") == group_name),
        None,
    )
    if group is None:
        return {"api_group": group_name, "discovered": False, "resources": []}
    version = group.get("preferredVersion", {}).get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9.]+", version):
        raise NodeAgentError("KUBERNETES_API_VERSION_INVALID")
    version_document = kubernetes_api_get(f"/apis/{group_name}/{version}")
    resources = []
    for resource_name in resource_names:
        resource = next(
            (
                item
                for item in version_document.get("resources", [])
                if item.get("name") == resource_name
            ),
            None,
        )
        if resource is None:
            resources.append({"resource": resource_name, "available": False, "items": []})
            continue
        if resource.get("namespaced"):
            path = f"/apis/{group_name}/{version}/namespaces/{namespace}/{resource_name}"
        else:
            path = f"/apis/{group_name}/{version}/{resource_name}"
        document = kubernetes_api_get(path)
        summaries = []
        for item in document.get("items", [])[:100]:
            metadata = item.get("metadata", {})
            summary = {
                "name": metadata.get("name"),
                "namespace": metadata.get("namespace"),
            }
            if group_name == "metal3.io":
                status_data = item.get("status", {})
                summary.update(
                    {
                        "online": item.get("spec", {}).get("online"),
                        "operational_status": status_data.get("operationalStatus"),
                        "provisioning_state": status_data.get("provisioning", {}).get("state"),
                        "powered_on": status_data.get("poweredOn"),
                        "error": str(status_data.get("errorMessage", ""))[:300],
                    }
                )
            else:
                status_data = item.get("status", {})
                summary.update(
                    {
                        "phase": status_data.get("phase"),
                        "version": item.get("spec", {}).get("version"),
                        "node_name": status_data.get("nodeRef", {}).get("name"),
                        "conditions": [
                            {
                                "type": condition.get("type"),
                                "status": condition.get("status"),
                                "reason": condition.get("reason"),
                            }
                            for condition in status_data.get("conditions", [])
                        ],
                    }
                )
            summaries.append(summary)
        resources.append(
            {
                "resource": resource_name,
                "available": True,
                "count": len(document.get("items", [])),
                "items": summaries,
            }
        )
    return {
        "api_group": group_name,
        "discovered": True,
        "version": version,
        "namespace": namespace,
        "resources": resources,
    }


def canonical_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def execute_command(command: dict[str, Any], node_type: str) -> dict[str, Any]:
    command_type = command.get("command_type")
    payload = command.get("payload", {})
    if not isinstance(payload, dict):
        raise NodeAgentError("INVALID_PAYLOAD")
    allowed = {
        "eph_node": {
            "node.health",
            "host.preflight",
            "cluster.plan",
            "cluster.health",
            "cluster.observe",
            "cluster.diagnose",
            "cluster.postflight",
            "k8s.resource.read",
            "capi.cluster.observe",
            "metal3.host.inspect",
        },
        "target_cluster": {
            "node.health",
            "cluster.health",
            "cluster.observe",
            "cluster.diagnose",
            "cluster.postflight",
            "k8s.resource.read",
        },
    }
    if command_type not in allowed.get(node_type, set()):
        raise NodeAgentError("CAPABILITY_NOT_CONFIGURED")
    if command_type == "node.health":
        return {
            "node_type": node_type,
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "agent_status": "ready",
        }
    if command_type == "cluster.health":
        version = kubernetes_api_get("/version")
        return {"api_reachable": True, "kubernetes_version": version.get("gitVersion")}
    if command_type == "cluster.observe":
        version = kubernetes_api_get("/version")
        nodes = kubernetes_api_get("/api/v1/nodes")
        api_groups = kubernetes_api_get("/apis")
        return {
            "kubernetes_version": version.get("gitVersion"),
            "node_count": len(nodes.get("items", [])),
            "node_names": [
                item.get("metadata", {}).get("name")
                for item in nodes.get("items", [])
            ],
            "api_group_count": len(api_groups.get("groups", [])),
        }
    if command_type == "cluster.postflight":
        nodes = kubernetes_api_get("/api/v1/nodes")
        items = nodes.get("items", [])
        not_ready = []
        for item in items:
            conditions = item.get("status", {}).get("conditions", [])
            ready = next(
                (
                    condition
                    for condition in conditions
                    if condition.get("type") == "Ready"
                ),
                {},
            )
            if ready.get("status") != "True":
                not_ready.append(item.get("metadata", {}).get("name"))
        return {
            "node_count": len(items),
            "ready": len(items) > 0 and not not_ready,
            "not_ready_nodes": not_ready,
        }
    if command_type == "cluster.diagnose":
        namespace = payload.get("namespace", "default")
        if not isinstance(namespace, str) or not namespace or "/" in namespace:
            raise NodeAgentError("INVALID_NAMESPACE")
        events = kubernetes_api_get(f"/api/v1/namespaces/{namespace}/events")
        return {
            "namespace": namespace,
            "event_count": len(events.get("items", [])),
            "warning_events": [
                sanitize_kubernetes_item("events", item)
                for item in events.get("items", [])
                if item.get("type") == "Warning"
            ][:100],
        }
    if command_type == "k8s.resource.read":
        resource = payload.get("resource")
        namespace = payload.get("namespace", "default")
        if resource in {"nodes", "namespaces"}:
            data = kubernetes_api_get(f"/api/v1/{resource}")
        elif (
            resource in {"pods", "events", "services"}
            and isinstance(namespace, str)
            and namespace
            and "/" not in namespace
        ):
            data = kubernetes_api_get(f"/api/v1/namespaces/{namespace}/{resource}")
        else:
            raise NodeAgentError("RESOURCE_NOT_ALLOWLISTED")
        return {
            "resource": resource,
            "namespace": namespace,
            "items": [
                sanitize_kubernetes_item(resource, item)
                for item in data.get("items", [])[:100]
            ],
        }
    if command_type == "capi.cluster.observe":
        namespace = payload.get("namespace", os.environ.get("AGENT_NAMESPACE", "default"))
        return inspect_api_group(
            "cluster.x-k8s.io",
            ["clusters", "machines", "machinedeployments"],
            namespace,
        )
    if command_type == "metal3.host.inspect":
        namespace = payload.get("namespace", os.environ.get("AGENT_NAMESPACE", "default"))
        return inspect_api_group("metal3.io", ["baremetalhosts"], namespace)
    if command_type == "host.preflight":
        disk = shutil.disk_usage("/")
        return {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "architecture": platform.machine(),
            "python_version": platform.python_version(),
            "cpu_count": os.cpu_count() or 1,
            "root_disk_total_bytes": disk.total,
            "root_disk_free_bytes": disk.free,
            "container_runtime": {
                "docker": shutil.which("docker") is not None,
                "containerd": shutil.which("containerd") is not None,
                "crictl": shutil.which("crictl") is not None,
            },
        }
    if command_type == "cluster.plan":
        spec = payload.get("cluster_spec")
        if not isinstance(spec, dict):
            raise NodeAgentError("CLUSTER_SPEC_REQUIRED")
        cluster_name = spec.get("cluster_name")
        kubernetes_version = spec.get("kubernetes_version")
        control_plane_replicas = spec.get("control_plane_replicas")
        worker_replicas = spec.get("worker_replicas")
        if not isinstance(cluster_name, str) or not cluster_name or len(cluster_name) > 63:
            raise NodeAgentError("INVALID_CLUSTER_NAME")
        if not isinstance(kubernetes_version, str) or not kubernetes_version.startswith("v1."):
            raise NodeAgentError("INVALID_KUBERNETES_VERSION")
        if (
            not isinstance(control_plane_replicas, int)
            or isinstance(control_plane_replicas, bool)
            or control_plane_replicas < 1
            or control_plane_replicas > 9
        ):
            raise NodeAgentError("INVALID_CONTROL_PLANE_REPLICAS")
        if (
            not isinstance(worker_replicas, int)
            or isinstance(worker_replicas, bool)
            or worker_replicas < 0
            or worker_replicas > 1000
        ):
            raise NodeAgentError("INVALID_WORKER_REPLICAS")
        return {
            "plan_status": "validated_dry_run",
            "cluster_name": cluster_name,
            "kubernetes_version": kubernetes_version,
            "control_plane_replicas": control_plane_replicas,
            "worker_replicas": worker_replicas,
            "plan_hash": canonical_hash(spec),
            "side_effects": False,
            "next_required_checks": [
                "management_kubernetes_version",
                "cluster_api_provider_versions",
                "metal3_crd_versions",
                "ironic_and_ipa_versions",
                "machine_template_compatibility",
                "network_and_image_configuration",
            ],
        }
    raise NodeAgentError("UNSUPPORTED_COMMAND")


def kubernetes_api_get(path: str) -> dict[str, Any]:
    allowed_prefixes = (
        "/version",
        "/api/v1/nodes",
        "/api/v1/namespaces",
        "/api/v1/namespaces/",
        "/apis",
    )
    if not path.startswith(allowed_prefixes) or ".." in path or "?" in path:
        raise NodeAgentError("KUBERNETES_PATH_NOT_ALLOWLISTED")
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
    port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    token_path = os.environ.get(
        "AGENT_SERVICE_ACCOUNT_TOKEN_FILE",
        "/var/run/secrets/kubernetes.io/serviceaccount/token",
    )
    ca_path = os.environ.get(
        "AGENT_SERVICE_ACCOUNT_CA_FILE",
        "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
    )
    if not host or not os.path.isfile(token_path) or not os.path.isfile(ca_path):
        raise NodeAgentError("KUBERNETES_IN_CLUSTER_IDENTITY_UNAVAILABLE")
    with open(token_path, encoding="utf-8") as token_file:
        token = token_file.read().strip()
    context = ssl.create_default_context(cafile=ca_path)
    request = urllib.request.Request(
        f"https://{host}:{port}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, context=context, timeout=15) as response:
            payload = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise NodeAgentError(f"KUBERNETES_API_HTTP_{exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise NodeAgentError("KUBERNETES_API_UNAVAILABLE") from exc
    if len(payload) > 1024 * 1024:
        raise NodeAgentError("KUBERNETES_RESPONSE_TOO_LARGE")
    try:
        result = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NodeAgentError("KUBERNETES_RESPONSE_INVALID_JSON") from exc
    if not isinstance(result, dict):
        raise NodeAgentError("KUBERNETES_RESPONSE_INVALID_SHAPE")
    return result


class ControlPlaneClient:
    def __init__(self, base_url: str, node_id: str, token: str, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.node_id = node_id
        self.token = token
        self.timeout = timeout

    def request(self, path: str, payload: dict[str, Any], lease_token: str = "") -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        if lease_token:
            headers["X-Agent-Lease"] = lease_token
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise NodeAgentError(f"CONTROL_PLANE_HTTP_{exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise NodeAgentError("CONTROL_PLANE_UNAVAILABLE") from exc

    def heartbeat(self, node_type: str) -> dict[str, Any]:
        capabilities = (
            [
                "node.health",
                "host.preflight",
                "cluster.plan",
                "cluster.health",
                "cluster.observe",
                "cluster.diagnose",
                "cluster.postflight",
                "k8s.resource.read",
                "capi.cluster.observe",
                "metal3.host.inspect",
            ]
            if node_type == "eph_node"
            else [
                "node.health",
                "cluster.health",
                "cluster.observe",
                "cluster.diagnose",
                "cluster.postflight",
                "k8s.resource.read",
            ]
        )
        return self.request(
            "/api/v1/agent-runtime/heartbeat/",
            {
                "node_id": self.node_id,
                "agent_version": "0.1.0",
                "capabilities": capabilities,
            },
        )

    def poll(self) -> dict[str, Any]:
        return self.request(
            "/api/v1/agent-runtime/commands/poll/",
            {"node_id": self.node_id},
        )

    def submit_result(
        self,
        command_id: str,
        lease_token: str,
        outcome: str,
        result: dict[str, Any],
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.request(
            f"/api/v1/agent-runtime/commands/{command_id}/result/",
            {
                "node_id": self.node_id,
                "outcome": outcome,
                "result": result,
                "error": error,
            },
            lease_token=lease_token,
        )


def touch_health_files(readiness: bool = False) -> None:
    Path(os.environ.get("AGENT_LIVENESS_FILE", "/tmp/agent-liveness")).touch()
    if readiness:
        Path(os.environ.get("AGENT_READINESS_FILE", "/tmp/agent-readiness")).touch()


def health_file_is_fresh(name: str, max_age_seconds: int = 45) -> bool:
    default_path = (
        "/tmp/agent-liveness"
        if name == "AGENT_LIVENESS_FILE"
        else "/tmp/agent-readiness"
    )
    path = Path(os.environ.get(name, default_path))
    if not path.is_file():
        return False
    return time.time() - path.stat().st_mtime <= max_age_seconds


def run_once(client: ControlPlaneClient, node_type: str) -> bool:
    client.heartbeat(node_type)
    envelope = client.poll()
    command = envelope.get("command")
    if not command:
        touch_health_files(readiness=True)
        return False
    lease_token = command.get("lease_token", "")
    command_id = command.get("id", "")
    try:
        result = execute_command(command, node_type)
        client.submit_result(command_id, lease_token, "succeeded", result)
    except NodeAgentError as exc:
        client.submit_result(
            command_id,
            lease_token,
            "failed",
            {},
            {"code": str(exc), "retryable": False},
        )
    touch_health_files(readiness=True)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Agent Studio remote node agent")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--readiness-check", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=5)
    args = parser.parse_args()
    if args.healthcheck:
        raise SystemExit(0 if health_file_is_fresh("AGENT_LIVENESS_FILE") else 1)
    if args.readiness_check:
        raise SystemExit(0 if health_file_is_fresh("AGENT_READINESS_FILE") else 1)
    base_url = os.environ.get("AGENT_CONTROL_URL", "").strip()
    node_id = os.environ.get("AGENT_NODE_ID", "").strip()
    token = os.environ.get("AGENT_TOKEN", "").strip()
    node_type = os.environ.get("AGENT_NODE_TYPE", "").strip()
    if not base_url or not node_id or not token:
        raise SystemExit("AGENT_CONTROL_URL, AGENT_NODE_ID and AGENT_TOKEN are required.")
    if node_type not in {"eph_node", "target_cluster"}:
        raise SystemExit("AGENT_NODE_TYPE must be eph_node or target_cluster.")
    if args.poll_seconds < 1 or args.poll_seconds > 300:
        raise SystemExit("--poll-seconds must be between 1 and 300.")
    if not base_url.startswith("https://") and os.environ.get("APP_ENV") == "production":
        raise SystemExit("Production node agents require HTTPS.")
    client = ControlPlaneClient(base_url, node_id, token)
    for _iteration in count():
        try:
            processed = run_once(client, node_type)
            if args.once:
                break
            if not processed:
                time.sleep(args.poll_seconds)
        except NodeAgentError as exc:
            touch_health_files()
            print(json.dumps({"event": "control_plane_error", "code": str(exc)}), flush=True)
            if args.once:
                raise SystemExit(1) from exc
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
