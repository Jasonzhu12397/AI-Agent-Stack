import pytest
from node_agent import network_inventory


def test_publish_inventory_creates_scoped_configmap(monkeypatch):
    calls = []
    configmap_name = network_inventory.configmap_name_for_node("worker-01")
    report = {
        "schema_version": "v1",
        "node_name": "worker-01",
        "node_uid": "node-uid-01",
        "collected_at": "2026-11-04T00:00:00+00:00",
        "hardware": {"status": "observed", "interfaces": [{"name": "eno1"}]},
    }

    def fake_request(path, method, payload=None):
        calls.append((path, method, payload))
        if path == "/api/v1/nodes/worker-01":
            return {"metadata": {"uid": "node-uid-01"}}
        if path.endswith("/" + configmap_name) and method == "GET":
            return None
        if path == "/api/v1/namespaces/ai-agent-inventory/configmaps" and method == "POST":
            return {}
        raise AssertionError((path, method))

    monkeypatch.setattr(network_inventory, "kubernetes_request", fake_request)
    monkeypatch.setattr(
        network_inventory,
        "build_inventory_report",
        lambda node_name, node_uid: report,
    )

    result = network_inventory.publish_inventory("worker-01")

    assert result == {
        "published": True,
        "node_name": "worker-01",
        "node_uid": "node-uid-01",
        "configmap_name": configmap_name,
        "interface_count": 1,
        "pci_network_device_count": 0,
    }
    create_call = calls[-1]
    assert create_call[1] == "POST"
    assert create_call[2]["data"]["inventory.json"]


def test_publish_inventory_updates_existing_configmap_with_resource_version(
    monkeypatch,
):
    calls = []
    configmap_name = network_inventory.configmap_name_for_node("worker-02")

    def fake_request(path, method, payload=None):
        calls.append((path, method, payload))
        if path == "/api/v1/nodes/worker-02":
            return {"metadata": {"uid": "node-uid-02"}}
        if path.endswith("/" + configmap_name) and method == "GET":
            return {"metadata": {"resourceVersion": "42"}}
        if path.endswith("/" + configmap_name) and method == "PUT":
            assert payload["metadata"]["resourceVersion"] == "42"
            return {}
        raise AssertionError((path, method))

    monkeypatch.setattr(network_inventory, "kubernetes_request", fake_request)
    monkeypatch.setattr(
        network_inventory,
        "build_inventory_report",
        lambda node_name, node_uid: {
            "schema_version": "v1",
            "node_name": node_name,
            "node_uid": node_uid,
            "hardware": {"status": "observed", "interfaces": [], "pci_network_devices": []},
        },
    )

    result = network_inventory.publish_inventory("worker-02")

    assert result["published"] is True
    assert calls[-1][1] == "PUT"


@pytest.mark.parametrize("node_name", ["", "Worker-01", "bad/name", "a" * 236])
def test_publish_inventory_rejects_invalid_node_names(node_name):
    with pytest.raises(network_inventory.NetworkInventoryError, match="INVALID_NODE_NAME"):
        network_inventory.publish_inventory(node_name)


def test_build_inventory_report_rejects_unavailable_host_sysfs(monkeypatch):
    monkeypatch.setattr(
        network_inventory,
        "inspect_host_network_hardware",
        lambda: {"status": "not_configured"},
    )

    with pytest.raises(
        network_inventory.NetworkInventoryError,
        match="HOST_SYSFS_INVENTORY_UNAVAILABLE",
    ):
        network_inventory.build_inventory_report("worker-01", "node-uid-01")


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/v1/nodes/worker-01", "GET"),
        ("/api/v1/namespaces/ai-agent-inventory/configmaps", "GET"),
        ("/api/v1/namespaces/ai-agent-inventory/configmaps", "POST"),
        (
            "/api/v1/namespaces/ai-agent-inventory/configmaps/"
            "network-inventory-0123456789abcdef0123",
            "GET",
        ),
        (
            "/api/v1/namespaces/ai-agent-inventory/configmaps/"
            "network-inventory-0123456789abcdef0123",
            "PUT",
        ),
    ],
)
def test_kubernetes_request_path_allowlist_accepts_expected_operations(path, method):
    network_inventory._validate_kubernetes_request_path(path, method)


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/v1/nodes/worker-01", "PUT"),
        ("/api/v1/nodes/worker-01/secrets", "GET"),
        ("/api/v1/nodes/%2e%2e/secrets", "GET"),
        ("/api/v1/namespaces/default/configmaps", "POST"),
        (
            "/api/v1/namespaces/ai-agent-inventory/configmaps/"
            "arbitrary-configmap",
            "PUT",
        ),
        (
            "/api/v1/namespaces/ai-agent-inventory/configmaps/"
            "network-inventory-0123456789abcdef0123",
            "POST",
        ),
        ("/api/v1/namespaces/ai-agent-inventory/configmaps?watch=1", "GET"),
    ],
)
def test_kubernetes_request_path_allowlist_rejects_unexpected_operations(path, method):
    with pytest.raises(
        network_inventory.NetworkInventoryError,
        match="KUBERNETES_PATH_NOT_ALLOWLISTED",
    ):
        network_inventory._validate_kubernetes_request_path(path, method)


def test_kubernetes_request_rejects_redirects_without_forwarding_bearer_token(
    monkeypatch,
    tmp_path,
):
    import ssl
    import urllib.error
    import urllib.request

    token_file = tmp_path / "token"
    ca_file = tmp_path / "ca.crt"
    token_file.write_text("service-account-token", encoding="utf-8")
    ca_file.write_text("test-ca", encoding="utf-8")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", "6443")
    monkeypatch.setenv("AGENT_SERVICE_ACCOUNT_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("AGENT_SERVICE_ACCOUNT_CA_FILE", str(ca_file))

    captured = {}

    class FakeOpener:
        def open(self, request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            raise urllib.error.HTTPError(
                request.full_url,
                302,
                "Found",
                {"Location": "https://attacker.example/collect"},
                None,
            )

    def fake_build_opener(*handlers):
        captured["handlers"] = handlers
        return FakeOpener()

    monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)
    monkeypatch.setattr(
        ssl,
        "create_default_context",
        lambda cafile=None: object(),
    )

    with pytest.raises(
        network_inventory.NetworkInventoryError,
        match="KUBERNETES_API_REDIRECT_REJECTED",
    ):
        network_inventory.kubernetes_request(
            "/api/v1/nodes/worker-01",
            "GET",
        )

    assert captured["request"].get_header("Authorization") == (
        "Bearer service-account-token"
    )
    assert captured["request"].full_url == (
        "https://10.0.0.1:6443/api/v1/nodes/worker-01"
    )
    assert captured["timeout"] == 15
    assert any(
        isinstance(handler, network_inventory._NoRedirectHandler)
        for handler in captured["handlers"]
    )


@pytest.mark.parametrize(
    "host,port",
    [
        ("https://attacker.example", "443"),
        ("api.example/path", "443"),
        ("api.example", "0"),
        ("api.example", "65536"),
        ("api.example", "443\\r\\nHost: attacker.example"),
    ],
)
def test_kubernetes_request_rejects_invalid_service_endpoint(
    monkeypatch,
    host,
    port,
):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", host)
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", port)
    with pytest.raises(
        network_inventory.NetworkInventoryError,
        match="KUBERNETES_SERVICE_ENDPOINT_INVALID",
    ):
        network_inventory.kubernetes_request(
            "/api/v1/nodes/worker-01",
            "GET",
        )
