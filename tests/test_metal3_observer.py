import pytest
from node_agent.metal3_observer import (
    Metal3ObservationError,
    observe_cluster_metal3,
)

VERSIONS = {
    "cluster.x-k8s.io": "v1beta1",
    "infrastructure.cluster.x-k8s.io": "v1beta1",
    "metal3.io": "v1alpha1",
}


def documents():
    machine = {
        "metadata": {"name": "worker-0", "uid": "machine-uid"},
        "spec": {
            "clusterName": "edge-lab",
            "providerID": "metal3://metal3-system/bmh-0/worker-0",
            "infrastructureRef": {
                "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
                "kind": "Metal3Machine",
                "name": "worker-0",
                "namespace": "default",
            },
        },
        "status": {
            "phase": "Running",
            "nodeRef": {"name": "node-0"},
        },
    }
    metal3_machine = {
        "metadata": {
            "name": "worker-0",
            "namespace": "default",
            "uid": "mm-uid",
        },
        "spec": {"providerID": "metal3://metal3-system/bmh-0/worker-0"},
        "status": {"ready": True},
    }
    host = {
        "metadata": {
            "name": "bmh-0",
            "namespace": "metal3-system",
            "uid": "bmh-uid",
        },
        "spec": {
            "online": True,
            "consumerRef": {
                "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
                "kind": "Metal3Machine",
                "name": "worker-0",
                "namespace": "default",
                "uid": "mm-uid",
            },
        },
        "status": {
            "operationalStatus": "OK",
            "provisioning": {"state": "provisioned"},
        },
    }
    return machine, metal3_machine, host


def run_observer(machine=None, metal3_machine=None, hosts=None, ironic_lookup=None):
    default_machine, default_mm, default_host = documents()
    machine = default_machine if machine is None else machine
    metal3_machine = default_mm if metal3_machine is None else metal3_machine
    hosts = [default_host] if hosts is None else hosts
    groups = [
        {"name": name, "preferredVersion": {"version": version}}
        for name, version in VERSIONS.items()
    ]
    node = {
        "metadata": {"name": "node-0", "uid": "node-uid"},
        "spec": {"providerID": "metal3://metal3-system/bmh-0/worker-0"},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    documents_by_path = {
        "/apis": {"groups": groups},
        (
            "/apis/infrastructure.cluster.x-k8s.io/v1beta1/namespaces/"
            "default/metal3machines/worker-0"
        ): metal3_machine,
    }

    def get_item(path):
        if path not in documents_by_path:
            raise AssertionError(path)
        return documents_by_path[path]

    def list_items(path):
        if path.endswith("/machines"):
            return machine if isinstance(machine, list) else [machine]
        if path.endswith("/baremetalhosts"):
            return hosts
        if path == "/api/v1/nodes":
            return [node]
        raise AssertionError(path)

    payload = {
        "cluster_name": "edge-lab",
        "namespace": "default",
        "baremetal_host_namespace": "metal3-system",
    }
    return observe_cluster_metal3(
        payload,
        get_item,
        list_items,
        ironic_lookup=ironic_lookup,
    )


def test_full_machine_metal3machine_bmh_chain_resolves():
    result = run_observer()
    assert result["ready"] is True
    assert result["machines"][0]["baremetal_host"] == "bmh-0"
    assert result["ironic"]["status"] == "not_configured"
    assert result["side_effects"] is False


def test_missing_infrastructure_reference_fails_closed():
    machine, _, _ = documents()
    machine["spec"].pop("infrastructureRef")
    result = run_observer(machine=machine)
    assert result["ready"] is False
    assert "machine_infrastructure_ref_missing" in result["blockers"]


def test_cross_namespace_infrastructure_reference_fails_closed():
    machine, _, _ = documents()
    machine["spec"]["infrastructureRef"]["namespace"] = "other"
    result = run_observer(machine=machine)
    assert result["ready"] is False
    assert "machine_infrastructure_ref_invalid_or_cross_namespace" in (
        result["blockers"]
    )


def test_missing_consumer_reference_fails_closed():
    _, _, host = documents()
    host["spec"].pop("consumerRef")
    result = run_observer(hosts=[host])
    assert result["ready"] is False
    assert "baremetal_host_consumer_not_found" in result["blockers"]


def test_ambiguous_consumer_reference_fails_closed():
    _, _, host = documents()
    duplicate = dict(host)
    duplicate["metadata"] = dict(
        host["metadata"],
        name="bmh-1",
        uid="bmh-uid-1",
    )
    result = run_observer(hosts=[host, duplicate])
    assert result["ready"] is False
    assert "baremetal_host_consumer_ambiguous" in result["blockers"]


@pytest.mark.parametrize(
    ("state", "online", "operational", "expected"),
    [
        ("provisioned", True, "OK", True),
        ("available", True, "OK", False),
        ("provisioning", True, "OK", False),
        ("provisioned", False, "OK", False),
        ("provisioned", True, "error", False),
    ],
)
def test_bmh_must_be_provisioned_and_healthy(
    state,
    online,
    operational,
    expected,
):
    _, _, host = documents()
    host["spec"]["online"] = online
    host["status"]["provisioning"]["state"] = state
    host["status"]["operationalStatus"] = operational
    assert run_observer(hosts=[host])["ready"] is expected


def test_consumer_reference_without_namespace_defaults_to_metal3machine_namespace():
    _, _, host = documents()
    host["spec"]["consumerRef"].pop("namespace")
    assert run_observer(hosts=[host])["ready"] is True


def test_consumer_reference_with_wrong_namespace_is_rejected():
    _, _, host = documents()
    host["spec"]["consumerRef"]["namespace"] = "wrong"
    result = run_observer(hosts=[host])
    assert result["ready"] is False
    assert "baremetal_host_consumer_not_found" in result["blockers"]


def test_bmh_missing_uid_fails_closed():
    _, _, host = documents()
    host["metadata"].pop("uid")
    with pytest.raises(
        Metal3ObservationError,
        match="BAREMETAL_HOST_IDENTITY_INVALID",
    ):
        run_observer(hosts=[host])


def test_ironic_active_node_is_verified_when_adapter_is_configured():
    _, _, host = documents()
    host["status"]["provisioning"]["ID"] = "12345678-1234-1234-1234-123456789abc"
    node = {
        "uuid": "12345678-1234-1234-1234-123456789abc",
        "provision_state": "active",
        "maintenance": False,
        "target_provision_state": None,
        "has_last_error": False,
    }
    result = run_observer(hosts=[host], ironic_lookup=lambda node_uuid: node)
    assert result["ready"] is True
    assert result["ironic"]["status"] == "verified"
    assert result["ironic"]["verified_node_count"] == 1


def test_ironic_maintenance_node_fails_closed():
    _, _, host = documents()
    host["status"]["provisioning"]["ID"] = "12345678-1234-1234-1234-123456789abc"
    node = {
        "uuid": "12345678-1234-1234-1234-123456789abc",
        "provision_state": "active",
        "maintenance": True,
        "target_provision_state": None,
        "has_last_error": False,
    }
    result = run_observer(hosts=[host], ironic_lookup=lambda node_uuid: node)
    assert result["ready"] is False
    assert result["ironic"]["status"] == "unhealthy"
    assert "ironic_node_not_active_or_unhealthy" in result["blockers"]


def test_ironic_enabled_requires_bmh_provisioning_uuid():
    _, _, host = documents()
    result = run_observer(hosts=[host], ironic_lookup=lambda node_uuid: {})
    assert result["ready"] is False
    assert "ironic_node_uuid_missing_or_invalid" in result["blockers"]


def test_ironic_api_errors_fail_closed_without_leaking_details():
    _, _, host = documents()

    def fail_lookup(node_uuid):
        raise RuntimeError("sensitive upstream detail")

    host["status"]["provisioning"]["ID"] = "12345678-1234-1234-1234-123456789abc"
    result = run_observer(hosts=[host], ironic_lookup=fail_lookup)
    assert result["ready"] is False
    assert "ironic_api_unavailable_or_node_unreadable" in result["blockers"]


def test_missing_ironic_adapter_is_never_reported_as_healthy():
    result = run_observer()
    assert result["ironic"]["status"] == "not_configured"


def test_malformed_ironic_adapter_identity_fails_closed():
    _, _, host = documents()
    host["status"]["provisioning"]["ID"] = (
        "12345678-1234-1234-1234-123456789abc"
    )
    node = {
        "uuid": None,
        "provision_state": "active",
        "maintenance": False,
        "target_provision_state": None,
        "has_last_error": False,
    }
    result = run_observer(hosts=[host], ironic_lookup=lambda node_uuid: node)
    assert result["ready"] is False
    assert result["ironic"]["status"] == "unhealthy"
    assert "ironic_node_not_active_or_unhealthy" in result["blockers"]


def test_machine_metal3machine_and_node_provider_ids_must_match():
    machine, metal3_machine, _ = documents()
    machine["spec"]["providerID"] = "metal3://other/worker-0/worker-0"
    result = run_observer(machine=machine, metal3_machine=metal3_machine)
    assert result["ready"] is False
    assert "capi_metal3_provider_id_mismatch" in result["blockers"]


def test_workload_node_provider_id_must_match_infrastructure_provider_id():
    result = run_observer()
    original = result["machines"][0]["node_provider_id"]
    assert original == "metal3://metal3-system/bmh-0/worker-0"


def test_metal3_machine_must_be_ready():
    _, metal3_machine, _ = documents()
    metal3_machine["status"]["ready"] = False
    result = run_observer(metal3_machine=metal3_machine)
    assert result["ready"] is False
    assert "metal3_machine_not_ready" in result["blockers"]


def test_capi_machine_must_be_running_and_reference_ready_node():
    machine, _, _ = documents()
    machine["status"]["phase"] = "Provisioning"
    result = run_observer(machine=machine)
    assert result["ready"] is False
    assert "capi_machine_not_running" in result["blockers"]


def test_workload_node_must_be_ready():
    result = run_observer()
    assert result["machines"][0]["node_ready"] is True
