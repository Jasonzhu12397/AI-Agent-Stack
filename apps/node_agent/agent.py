import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import time
import urllib.error
import urllib.request
from itertools import count
from typing import Any


class NodeAgentError(Exception):
    pass


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
        "eph_node": {"node.health", "host.preflight", "cluster.plan"},
        "target_cluster": {"node.health"},
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
            ["node.health", "host.preflight", "cluster.plan"]
            if node_type == "eph_node"
            else ["node.health"]
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


def run_once(client: ControlPlaneClient, node_type: str) -> bool:
    client.heartbeat(node_type)
    envelope = client.poll()
    command = envelope.get("command")
    if not command:
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
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Agent Studio remote node agent")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=5)
    args = parser.parse_args()
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
    for iteration in count():
        try:
            processed = run_once(client, node_type)
            if args.once:
                break
            if not processed:
                time.sleep(args.poll_seconds)
        except NodeAgentError as exc:
            print(json.dumps({"event": "control_plane_error", "code": str(exc)}), flush=True)
            if args.once:
                raise SystemExit(1) from exc
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
