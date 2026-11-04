import argparse
import hashlib
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from itertools import count

from .agent import inspect_host_network_hardware


class NetworkInventoryError(Exception):
    pass


def kubernetes_request(path, method, payload=None):
    if method not in {"GET", "POST", "PUT"}:
        raise NetworkInventoryError("UNSUPPORTED_KUBERNETES_METHOD")
    allowed_prefixes = (
        "/api/v1/nodes/",
        "/api/v1/namespaces/ai-agent-system/configmaps",
    )
    if not path.startswith(allowed_prefixes) or ".." in path:
        raise NetworkInventoryError("KUBERNETES_PATH_NOT_ALLOWLISTED")
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
        raise NetworkInventoryError("KUBERNETES_IN_CLUSTER_IDENTITY_UNAVAILABLE")
    with open(token_path, encoding="utf-8") as token_file:
        token = token_file.read().strip()
    context = ssl.create_default_context(cafile=ca_path)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"https://{host}:{port}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, context=context, timeout=15) as response:
            raw = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and method == "GET":
            return None
        raise NetworkInventoryError(f"KUBERNETES_API_HTTP_{exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise NetworkInventoryError("KUBERNETES_API_UNAVAILABLE") from exc
    if len(raw) > 1024 * 1024:
        raise NetworkInventoryError("KUBERNETES_RESPONSE_TOO_LARGE")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NetworkInventoryError("KUBERNETES_RESPONSE_INVALID_JSON") from exc
    if not isinstance(result, dict):
        raise NetworkInventoryError("KUBERNETES_RESPONSE_INVALID_SHAPE")
    return result


def build_inventory_report(node_name, node_uid):
    hardware = inspect_host_network_hardware()
    if hardware.get("status") != "observed":
        raise NetworkInventoryError("HOST_SYSFS_INVENTORY_UNAVAILABLE")
    return {
        "schema_version": "v1",
        "node_name": node_name,
        "node_uid": node_uid,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "hardware": hardware,
    }


def configmap_name_for_node(node_name):
    digest = hashlib.sha256(node_name.encode("utf-8")).hexdigest()[:20]
    return "network-inventory-" + digest


def publish_inventory(node_name):
    if (
        not isinstance(node_name, str)
        or len(node_name) > 235
        or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", node_name)
    ):
        raise NetworkInventoryError("INVALID_NODE_NAME")
    encoded_node_name = urllib.parse.quote(node_name, safe="")
    node = kubernetes_request(f"/api/v1/nodes/{encoded_node_name}", "GET")
    if not isinstance(node, dict):
        raise NetworkInventoryError("KUBERNETES_NODE_NOT_FOUND")
    node_uid = node.get("metadata", {}).get("uid")
    if not isinstance(node_uid, str) or not node_uid:
        raise NetworkInventoryError("KUBERNETES_NODE_UID_MISSING")
    report = build_inventory_report(node_name, node_uid)
    configmap_name = configmap_name_for_node(node_name)
    encoded_configmap_name = urllib.parse.quote(configmap_name, safe="")
    configmap_path = (
        "/api/v1/namespaces/ai-agent-system/configmaps/"
        + encoded_configmap_name
    )
    existing = kubernetes_request(configmap_path, "GET")
    metadata = {
        "name": configmap_name,
        "namespace": "ai-agent-system",
        "labels": {"app.kubernetes.io/name": "network-inventory-agent"},
    }
    if isinstance(existing, dict):
        resource_version = existing.get("metadata", {}).get("resourceVersion")
        if not isinstance(resource_version, str) or not resource_version:
            raise NetworkInventoryError("CONFIGMAP_RESOURCE_VERSION_MISSING")
        metadata["resourceVersion"] = resource_version
        method = "PUT"
        path = configmap_path
    else:
        method = "POST"
        path = "/api/v1/namespaces/ai-agent-system/configmaps"
    document = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": metadata,
        "data": {"inventory.json": json.dumps(report, separators=(",", ":"))},
    }
    kubernetes_request(path, method, document)
    return {
        "published": True,
        "node_name": node_name,
        "node_uid": node_uid,
        "configmap_name": configmap_name,
        "interface_count": len(report["hardware"].get("interfaces", [])),
        "pci_network_device_count": len(
            report["hardware"].get("pci_network_devices", [])
        ),
    }


def main():
    parser = argparse.ArgumentParser(description="Read-only physical network inventory publisher")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=60)
    args = parser.parse_args()
    node_name = os.environ.get("NODE_NAME", "").strip()
    if not node_name:
        raise SystemExit("NODE_NAME is required.")
    if args.poll_seconds < 10 or args.poll_seconds > 3600:
        raise SystemExit("--poll-seconds must be between 10 and 3600.")
    for _iteration in count():
        try:
            result = publish_inventory(node_name)
            print(json.dumps({"event": "network_inventory_published", **result}), flush=True)
        except NetworkInventoryError as exc:
            print(json.dumps({"event": "network_inventory_error", "code": str(exc)}), flush=True)
            if args.once:
                raise SystemExit(1) from exc
        if args.once:
            break
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
