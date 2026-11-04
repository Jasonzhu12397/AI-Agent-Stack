import pytest

from node_agent import network_inventory


def test_publish_inventory_creates_scoped_configmap(monkeypatch):
    calls = []
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
        if path.endswith("/network-inventory-worker-01") and method == "GET":
            return None
        if path == "/api/v1/namespaces/ai-agent-system/configmaps" and method == "POST":
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
        "configmap_name": "network-inventory-worker-01",
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

    def fake_request(path, method, payload=None):
        calls.append((path, method, payload))
        if path == "/api/v1/nodes/worker-02":
            return {"metadata": {"uid": "node-uid-02"}}
        if path.endswith("/network-inventory-worker-02") and method == "GET":
            return {"metadata": {"resourceVersion": "42"}}
        if path.endswith("/network-inventory-worker-02") and method == "PUT":
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
