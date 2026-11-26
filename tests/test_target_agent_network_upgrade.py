"""Network bundle upgrade keeps a real revision and rollback replays it."""

import copy
import json
from pathlib import Path

import pytest
import yaml

from apps.target_agent.network_upgrade import (
    NetworkUpgradeError,
    NetworkUpgradeExecutionError,
    execute_network_revisions,
    execute_network_rollback,
    execute_network_upgrade,
)


def _deployment(version, replicas=3):
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "calico-node",
            "namespace": "default",
            "annotations": {
                "ai-agent-stack.io/network-version": version,
                "ai-agent-stack.io/network-plugin": "calico",
            },
        },
        "spec": {"replicas": replicas},
        "status": {"readyReplicas": replicas, "availableReplicas": replicas},
    }


def _service():
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "calico-metrics"},
        "spec": {"ports": [{"port": 9090}]},
    }


class FakeKubectl:
    def __init__(self):
        self.objects = {
            ("Deployment", "default", "calico-node"): _deployment("1.0.0")
        }
        self.configmaps = {}
        self.calls = []
        self.apply_records = []
        self.mutations = 0

    def _lookup(self, kind, name, namespace):
        return self.objects.get((kind, namespace, name))

    def __call__(self, arguments, **kwargs):
        self.calls.append((list(arguments), kwargs))
        if arguments[0] == "get" and arguments[1] == "configmaps":
            selector = ""
            if "--selector" in arguments:
                selector = arguments[arguments.index("--selector") + 1]
            items = []
            for configmap in self.configmaps.values():
                labels = configmap.get("metadata", {}).get("labels", {})
                if selector and any(
                    f"{key}={value}" == selector for key, value in labels.items()
                ):
                    items.append(configmap)
            return {
                "available": True,
                "returncode": 0,
                "stdout": json.dumps({"kind": "ConfigMapList", "items": items}),
            }
        if arguments[0] == "get":
            kind, name = arguments[1], arguments[2]
            namespace = ""
            if "--namespace" in arguments:
                namespace = arguments[arguments.index("--namespace") + 1]
            if kind == "ConfigMap" and name in self.configmaps:
                return {
                    "available": True,
                    "returncode": 0,
                    "stdout": json.dumps(self.configmaps[name]),
                }
            document = self._lookup(kind, name, namespace)
            if document is None:
                return {
                    "available": True,
                    "returncode": 404,
                    "stdout": "",
                    "stderr": "Error from server (NotFound)",
                }
            return {"available": True, "returncode": 0, "stdout": json.dumps(document)}
        if arguments[0] == "apply":
            dry_run = "--dry-run=server" in arguments
            path = Path(arguments[arguments.index("-f") + 1])
            documents = list(yaml.safe_load_all(path.read_text()))
            self.apply_records.append(
                ([document.get("kind") for document in documents], dry_run)
            )
            if not dry_run:
                self.mutations += 1
                for document in documents:
                    kind = document["kind"]
                    name = document["metadata"]["name"]
                    namespace = document["metadata"].get("namespace", "")
                    if kind == "ConfigMap":
                        self.configmaps[name] = document
                    elif kind in ("Deployment", "DaemonSet"):
                        stored = copy.deepcopy(document)
                        replicas = stored["spec"].get("replicas", 1)
                        stored["status"] = {
                            "readyReplicas": replicas,
                            "availableReplicas": replicas,
                        }
                        self.objects[(kind, namespace, name)] = stored
                    else:
                        self.objects[(kind, namespace, name)] = copy.deepcopy(document)
            return {"available": True, "returncode": 0, "stdout": "applied\n"}
        if arguments[0] == "delete":
            kind, name = arguments[1], arguments[2]
            namespace = ""
            if "--namespace" in arguments:
                namespace = arguments[arguments.index("--namespace") + 1]
            if "--dry-run=server" in arguments:
                return {"available": True, "returncode": 0, "stdout": "dry-run\n"}
            self.mutations += 1
            self.objects.pop((kind, namespace, name), None)
            return {"available": True, "returncode": 0, "stdout": "deleted\n"}
        raise AssertionError(f"unexpected kubectl call: {arguments}")


def _upgrade_payload(mode="simulation"):
    return {
        "target_cluster_name": "cluster-1",
        "target_namespace": "default",
        "plugin": "calico",
        "target_version": "1.1.0",
        "execution_mode": mode,
        "reason": "upgrade calico",
        "documents": yaml.safe_dump_all([_deployment("1.1.0")]),
    }


def _rollback_payload(mode="execute"):
    return {
        "target_cluster_name": "cluster-1",
        "target_namespace": "default",
        "plugin": "calico",
        "execution_mode": mode,
        "reason": "rollback calico",
    }


def test_simulation_reads_current_objects_without_submitting_a_revision():
    kubectl = FakeKubectl()
    outcome = execute_network_upgrade(
        _upgrade_payload(), kubectl, cluster_name="cluster-1"
    )
    assert outcome["from_version"] == "1.0.0"
    assert outcome["target_version"] == "1.1.0"
    assert outcome["object_count"] == 1
    assert outcome["objects"][0]["exists"] is True
    assert outcome["upgrade_simulated"] is True
    assert outcome["upgrade_performed"] is False
    assert outcome["side_effects"] is False
    assert kubectl.mutations == 0
    assert not kubectl.configmaps


def test_execute_creates_the_revision_before_applying_the_new_bundle():
    kubectl = FakeKubectl()
    outcome = execute_network_upgrade(
        _upgrade_payload("execute"), kubectl, cluster_name="cluster-1"
    )
    assert outcome["upgrade_performed"] is True
    assert outcome["side_effects"] is True
    assert outcome["revision"]
    assert kubectl.objects[("Deployment", "default", "calico-node")]["metadata"][
        "annotations"
    ]["ai-agent-stack.io/network-version"] == "1.1.0"
    revision = kubectl.configmaps["ai-agent-stack-netrev-calico"]
    snapshot = yaml.safe_load(revision["data"]["snapshot"])
    assert snapshot["previous_version"] == "1.0.0"
    assert snapshot["objects"][0]["exists"] is True
    applies = [call[0] for call in kubectl.calls if call[0][0] == "apply"]
    assert "--dry-run=server" in applies[0]
    assert "--dry-run=server" not in applies[1]


def test_rollback_restores_the_captured_document():
    kubectl = FakeKubectl()
    execute_network_upgrade(_upgrade_payload("execute"), kubectl, cluster_name="cluster-1")
    outcome = execute_network_rollback(
        _rollback_payload(), kubectl, cluster_name="cluster-1"
    )
    assert outcome["rollback_performed"] is True
    assert outcome["previous_version"] == "1.0.0"
    current = kubectl.objects[("Deployment", "default", "calico-node")]
    assert current["metadata"]["annotations"]["ai-agent-stack.io/network-version"] == "1.0.0"


def test_rollback_removes_an_object_the_upgrade_introduced():
    docs = yaml.safe_dump_all([_deployment("1.1.0"), _service()])
    payload = {**_upgrade_payload("execute"), "documents": docs}
    kubectl = FakeKubectl()
    execute_network_upgrade(payload, kubectl, cluster_name="cluster-1")
    assert ("Service", "default", "calico-metrics") in kubectl.objects
    outcome = execute_network_rollback(
        _rollback_payload(), kubectl, cluster_name="cluster-1"
    )
    assert outcome["removed_objects"][0]["kind"] == "Service"
    assert ("Service", "default", "calico-metrics") not in kubectl.objects


def test_rollback_without_a_revision_fails_before_touching_anything():
    kubectl = FakeKubectl()
    with pytest.raises(NetworkUpgradeExecutionError) as caught:
        execute_network_rollback(_rollback_payload(), kubectl, cluster_name="cluster-1")
    assert caught.value.code == "NETWORK_UPGRADE_REVISION_NOT_FOUND"
    assert kubectl.mutations == 0


def test_a_secret_inside_the_bundle_is_rejected():
    payload = {
        **_upgrade_payload("execute"),
        "documents": yaml.safe_dump_all(
            [
                _deployment("1.1.0"),
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": "credentials"},
                    "stringData": {"token": "should-not-travel"},
                },
            ]
        ),
    }
    kubectl = FakeKubectl()
    with pytest.raises(NetworkUpgradeExecutionError) as caught:
        execute_network_upgrade(payload, kubectl, cluster_name="cluster-1")
    assert caught.value.code == "NETWORK_UPGRADE_BUNDLE_FORBIDDEN_KIND"
    assert kubectl.mutations == 0


def test_each_upgrade_keeps_an_immutable_revision_for_older_rollback():
    kubectl = FakeKubectl()
    first = execute_network_upgrade(
        _upgrade_payload("execute"), kubectl, cluster_name="cluster-1"
    )
    first_revision = first["revision"]
    first_config = first["revision_config_map"]
    assert first_config in kubectl.configmaps

    second_payload = {
        **_upgrade_payload("execute"),
        "target_version": "1.2.0",
        "documents": yaml.safe_dump_all([_deployment("1.2.0")]),
    }
    second = execute_network_upgrade(second_payload, kubectl, cluster_name="cluster-1")
    assert second["revision"] != first_revision
    assert kubectl.configmaps["ai-agent-stack-netrev-calico"]["data"][
        "latest_revision"
    ] == second["revision"]
    assert first_config in kubectl.configmaps

    rolled_back = execute_network_rollback(
        {**_rollback_payload(), "revision": first_revision},
        kubectl,
        cluster_name="cluster-1",
    )
    assert rolled_back["revision"] == first_revision
    current = kubectl.objects[("Deployment", "default", "calico-node")]
    assert current["metadata"]["annotations"]["ai-agent-stack.io/network-version"] == "1.0.0"


def test_revision_collision_gets_a_unique_history_name(monkeypatch):
    kubectl = FakeKubectl()
    monkeypatch.setattr(
        "apps.target_agent.network_upgrade.time.strftime",
        lambda *_args: "20261126T000000Z",
    )
    first = execute_network_upgrade(
        _upgrade_payload("execute"), kubectl, cluster_name="cluster-1"
    )
    second = execute_network_upgrade(
        {**_upgrade_payload("execute"), "target_version": "1.2.0"},
        kubectl,
        cluster_name="cluster-1",
    )
    assert first["revision"] == "20261126T000000Z"
    assert second["revision"] == "20261126T000000Z-2"
    assert second["revision_config_map"].endswith("20261126t000000z-2")


def test_revision_listing_reports_history_newest_first():
    kubectl = FakeKubectl()
    first = execute_network_upgrade(
        _upgrade_payload("execute"), kubectl, cluster_name="cluster-1"
    )
    second = execute_network_upgrade(
        {**_upgrade_payload("execute"), "target_version": "1.2.0"},
        kubectl,
        cluster_name="cluster-1",
    )
    listing = execute_network_revisions(
        {
            "target_cluster_name": "cluster-1",
            "target_namespace": "default",
            "plugin": "calico",
        },
        kubectl,
        cluster_name="cluster-1",
    )
    revisions = [row["revision"] for row in listing["revisions"]]
    assert listing["revision_count"] == 2
    assert listing["latest_revision"] == second["revision"]
    assert revisions[0] == second["revision"]
    assert first["revision"] in revisions
    assert all(row["object_count"] == 1 for row in listing["revisions"])


def test_revision_listing_is_empty_before_any_upgrade():
    listing = execute_network_revisions(
        {
            "target_cluster_name": "cluster-1",
            "target_namespace": "default",
            "plugin": "calico",
        },
        FakeKubectl(),
        cluster_name="cluster-1",
    )
    assert listing["revision_count"] == 0
    assert listing["latest_revision"] is None
    assert listing["side_effects"] is False


def test_rollback_revision_must_be_a_captured_id():
    kubectl = FakeKubectl()
    with pytest.raises(NetworkUpgradeError) as caught:
        execute_network_rollback(
            {**_rollback_payload(), "revision": "../other-configmap"},
            kubectl,
            cluster_name="cluster-1",
        )
    assert caught.value.code == "NETWORK_UPGRADE_REQUEST_INVALID"
    assert kubectl.mutations == 0


def _crd():
    return {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {"name": "caliconetworkpolicies.crd.projectcalico.org"},
        "spec": {
            "group": "crd.projectcalico.org",
            "scope": "Namespaced",
            "names": {"kind": "CalicoNetworkPolicy", "plural": "caliconetworkpolicies"},
            "versions": [{"name": "v1", "served": True, "storage": True}],
        },
    }


def _custom_resource():
    return {
        "apiVersion": "crd.projectcalico.org/v1",
        "kind": "CalicoNetworkPolicy",
        "metadata": {"name": "default-deny", "namespace": "default"},
        "spec": {"selector": "all()"},
    }


def test_crd_bundle_applies_the_definition_before_validating_its_resources():
    docs = yaml.safe_dump_all([_deployment("1.1.0"), _crd(), _custom_resource()])
    kubectl = FakeKubectl()
    outcome = execute_network_upgrade(
        {**_upgrade_payload("execute"), "documents": docs},
        kubectl,
        cluster_name="cluster-1",
    )
    assert outcome["crds_applied_first"] is True
    crd_apply = next(
        index
        for index, (kinds, dry_run) in enumerate(kubectl.apply_records)
        if "CustomResourceDefinition" in kinds and dry_run is False
    )
    custom_dry_run = next(
        index
        for index, (kinds, dry_run) in enumerate(kubectl.apply_records)
        if "CalicoNetworkPolicy" in kinds and dry_run is True
    )
    custom_apply = next(
        index
        for index, (kinds, dry_run) in enumerate(kubectl.apply_records)
        if "CalicoNetworkPolicy" in kinds and dry_run is False
    )
    assert crd_apply < custom_dry_run < custom_apply


def test_rollback_removes_custom_resources_before_their_crd():
    docs = yaml.safe_dump_all([_deployment("1.1.0"), _crd(), _custom_resource()])
    kubectl = FakeKubectl()
    execute_network_upgrade(
        {**_upgrade_payload("execute"), "documents": docs},
        kubectl,
        cluster_name="cluster-1",
    )
    execute_network_rollback(_rollback_payload(), kubectl, cluster_name="cluster-1")
    real_deletes = [
        arguments[1]
        for arguments, _kwargs in kubectl.calls
        if arguments[0] == "delete" and "--dry-run=server" not in arguments
    ]
    assert real_deletes.index("CalicoNetworkPolicy") < real_deletes.index(
        "CustomResourceDefinition"
    )
