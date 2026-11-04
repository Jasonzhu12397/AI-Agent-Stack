import pytest

from apps.node_agent.metal3_observer import observe_cluster_metal3


VERSIONS = {"cluster.x-k8s.io": "v1beta1", "infrastructure.cluster.x-k8s.io": "v1beta1", "metal3.io": "v1alpha1"}


def documents():
    machine = {"metadata": {"name": "worker-0", "uid": "machine-uid"}, "spec": {"clusterName": "edge-lab", "infrastructureRef": {"apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1", "kind": "Metal3Machine", "name": "worker-0", "namespace": "default"}}}
    mm = {"metadata": {"name": "worker-0", "namespace": "default", "uid": "mm-uid"}, "status": {"ready": True}}
    host = {"metadata": {"name": "bmh-0", "namespace": "metal3-system", "uid": "bmh-uid"}, "spec": {"online": True, "consumerRef": {"apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1", "kind": "Metal3Machine", "name": "worker-0", "namespace": "default", "uid": "mm-uid"}}, "status": {"operationalStatus": "OK", "provisioning": {"state": "provisioned"}}}
    return machine, mm, host


def run_observer(machine=None, mm=None, hosts=None):
    default_machine, default_mm, default_host = documents()
    machine = default_machine if machine is None else machine
    mm = default_mm if mm is None else mm
    hosts = [default_host] if hosts is None else hosts
    docs = {"/apis": {"groups": [{"name": k, "preferredVersion": {"version": v}} for k, v in VERSIONS.items()]}, "/apis/infrastructure.cluster.x-k8s.io/v1beta1/namespaces/default/metal3machines/worker-0": mm}
    def get_item(path):
        if path not in docs:
            raise AssertionError(path)
        return docs[path]
    def list_items(path):
        if path.endswith("/machines"):
            return machine if isinstance(machine, list) else [machine]
        if path.endswith("/baremetalhosts"):
            return hosts
        raise AssertionError(path)
    return observe_cluster_metal3({"cluster_name": "edge-lab", "namespace": "default", "baremetal_host_namespace": "metal3-system"}, get_item, list_items)


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
    assert "machine_infrastructure_ref_invalid_or_cross_namespace" in result["blockers"]


def test_missing_consumer_reference_fails_closed():
    _, _, host = documents()
    host["spec"].pop("consumerRef")
    result = run_observer(hosts=[host])
    assert result["ready"] is False
    assert "baremetal_host_consumer_not_found" in result["blockers"]


def test_ambiguous_consumer_reference_fails_closed():
    _, _, host = documents()
    duplicate = dict(host)
    duplicate["metadata"] = dict(host["metadata"], name="bmh-1", uid="bmh-uid-1")
    result = run_observer(hosts=[host, duplicate])
    assert result["ready"] is False
    assert "baremetal_host_consumer_ambiguous" in result["blockers"]


@pytest.mark.parametrize(("state", "online", "operational", "expected"), [("provisioned", True, "OK", True), ("available", True, "OK", False), ("provisioning", True, "OK", False), ("provisioned", False, "OK", False), ("provisioned", True, "error", False)])
def test_bmh_must_be_provisioned_and_healthy(state, online, operational, expected):
    _, _, host = documents()
    host["spec"]["online"] = online
    host["status"]["provisioning"]["state"] = state
    host["status"]["operationalStatus"] = operational
    assert run_observer(hosts=[host])["ready"] is expected


def test_missing_ironic_adapter_is_never_reported_as_healthy():
    assert run_observer()["ironic"]["status"] == "not_configured"
