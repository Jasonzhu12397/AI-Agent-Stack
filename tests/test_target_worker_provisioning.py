from __future__ import annotations

import json

import yaml

from apps.target_agent import main as target_main
from apps.target_agent.ntp_policy import render_worker_chrony_pre_kubeadm_command
from apps.target_agent.worker_provisioning import (
    WorkerProvisioningError,
    _verify_sriov_configmap_ownership,
    execute_worker_provisioning,
)


def _resources(replicas=2):
    return [
        {
            "apiVersion": "cluster.x-k8s.io/v1beta2",
            "kind": "MachineDeployment",
            "metadata": {
                "name": "workers",
                "namespace": "metal3",
                "labels": {
                    "cluster.x-k8s.io/cluster-name": "target",
                    "nodepool": "workers",
                },
            },
            "spec": {
                "clusterName": "target",
                "replicas": replicas,
                "selector": {
                    "matchLabels": {
                        "cluster.x-k8s.io/cluster-name": "target",
                        "nodepool": "workers",
                    },
                },
                "template": {
                    "metadata": {
                        "labels": {
                            "cluster.x-k8s.io/cluster-name": "target",
                            "nodepool": "workers",
                        },
                    },
                    "spec": {
                        "bootstrap": {
                            "configRef": {
                                "apiGroup": "bootstrap.cluster.x-k8s.io",
                                "kind": "KubeadmConfigTemplate",
                                "name": "workers-bootstrap",
                            },
                        },
                        "infrastructureRef": {
                            "apiGroup": "infrastructure.cluster.x-k8s.io",
                            "kind": "Metal3MachineTemplate",
                            "name": "workers-infra",
                        },
                        "version": "v1.37.0",
                    },
                },
            },
        },
        {
            "apiVersion": "bootstrap.cluster.x-k8s.io/v1beta2",
            "kind": "KubeadmConfigTemplate",
            "metadata": {"name": "workers-bootstrap", "namespace": "metal3"},
            "spec": {
                "template": {
                    "spec": {
                        "joinConfiguration": {
                            "nodeRegistration": {"name": "{{ ds.meta_data.name }}"},
                        },
                        "preKubeadmCommands": [],
                    },
                },
            },
        },
        {
            "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
            "kind": "Metal3MachineTemplate",
            "metadata": {"name": "workers-infra", "namespace": "metal3"},
            "spec": {
                "nodeReuse": False,
                "template": {
                    "spec": {
                        "automatedCleaningMode": "metadata",
                        "image": {
                            "url": "https://images.example.invalid/worker.raw",
                            "checksum": "a" * 64,
                            "checksumType": "sha256",
                            "format": "raw",
                        },
                        "dataTemplate": {"name": "workers-network"},
                    },
                },
            },
        },
        {
            "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
            "kind": "Metal3DataTemplate",
            "metadata": {"name": "workers-network", "namespace": "metal3"},
            "spec": {
                "clusterName": "target",
                "metaData": {
                    "objectNames": [
                        {"key": "machine_name", "object": "machine"},
                        {"key": "bmh_name", "object": "baremetalhost"},
                    ],
                },
                "networkData": {
                    "links": {
                        "ethernets": [
                            {"type": "phy", "id": "eno1"},
                            {"type": "phy", "id": "ens1f0"},
                        ],
                    },
                    "networks": {
                        "ipv4DHCP": [{"id": "control", "link": "eno1"}],
                        "ipv4": [{
                            "id": "data",
                            "link": "ens1f0",
                            "ipAddressFromIPPool": "workers-data",
                        }],
                    },
                    "services": {"dns": ["10.0.0.53"]},
                },
            },
        },
    ]


def _network_contract():
    return {
        "control_plane_interface": "eno1",
        "data_plane_interface": "ens1f0",
        "control_plane_cidr": "10.0.0.0/24",
        "data_plane_cidr": "10.1.0.0/24",
        "data_plane_mtu": 9000,
        "pod_traffic_network": "data",
        "calico_enabled": True,
        "multus_enabled": True,
        "ovs_enabled": True,
        "sriov_cni_enabled": True,
        "sriov_dpdk_enabled": True,
        "required_network_attachments": [
            {
                "namespace": "metal3",
                "name": "data-sriov",
                "cni_type": "sriov",
                "resource_name": "intel.com/sriov_dpdk",
            },
            {
                "namespace": "metal3",
                "name": "data-ovs",
                "cni_type": "ovs",
                "resource_name": None,
            },
        ],
    }


def _proof():
    return {
        "eph_pivot_attestation": {
            "source_cluster_uid": "source-uid",
            "target_cluster_uid": "target-uid",
        },
    }


def _runner_factory(available_bmhs=2, existing_machine_deployment=None, updated_replicas=2, missing_attachments=None):
    calls = []
    applied = {}
    cluster = {
        "metadata": {
            "uid": "target-uid",
            "annotations": {"ai-agent-stack.io/source-cluster-uid": "source-uid"},
        },
        "spec": {
            "controlPlaneRef": {
                "apiGroup": "controlplane.cluster.x-k8s.io",
                "kind": "KubeadmControlPlane",
                "name": "target-control-plane",
            },
        },
    }
    existing = existing_machine_deployment or {}

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[0] == "get" and arguments[1] == "clusters.cluster.x-k8s.io":
            return {"available": True, "stdout": json.dumps(cluster), "stderr": ""}
        if (
            arguments[0] == "get"
            and arguments[1] == "kubeadmcontrolplanes.controlplane.cluster.x-k8s.io"
        ):
            return {
                "available": True,
                "stdout": json.dumps({"spec": {"version": "v1.37.0"}}),
                "stderr": "",
            }
        if arguments[0] == "get" and arguments[1] == "crd":
            group = arguments[2].split(".", 1)[1]
            kind = arguments[2].split(".", 1)[0]
            version = {
                "machinedeployments": "v1beta2",
                "kubeadmconfigtemplates": "v1beta2",
                "metal3machinetemplates": "v1beta1",
                "metal3datatemplates": "v1beta1",
            }[kind]
            return {
                "available": True,
                "stdout": json.dumps({
                    "spec": {
                        "group": group,
                        "versions": [{
                            "name": version,
                            "served": True,
                            "schema": {"openAPIV3Schema": {"type": "object"}},
                        }],
                    },
                }),
                "stderr": "",
            }
        if arguments[0] == "get" and arguments[1] == "ippools.ipam.metal3.io":
            return {
                "available": True,
                "stdout": json.dumps({
                    "items": [{
                        "metadata": {"name": "workers-data"},
                        "spec": {"clusterName": "target"},
                    }],
                }),
                "stderr": "",
            }
        if (
            arguments[0] == "get"
            and arguments[1] == "network-attachment-definitions.k8s.cni.cncf.io"
        ):
            name = arguments[2]
            if name in (missing_attachments or set()):
                return {
                    "available": True,
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "Error from server (NotFound)",
                }
            configs = {
                "data-sriov": {
                    "cniVersion": "0.3.1",
                    "name": "data-sriov",
                    "type": "sriov",
                    "resourceName": "intel.com/sriov_dpdk",
                },
                "data-ovs": {
                    "cniVersion": "0.3.1",
                    "name": "data-ovs",
                    "type": "ovs",
                },
            }
            if name not in configs:
                return {"available": True, "returncode": 1, "stdout": "", "stderr": "NotFound"}
            return {
                "available": True,
                "returncode": 0,
                "stdout": json.dumps({
                    "metadata": {"name": name, "namespace": "metal3"},
                    "spec": {"config": json.dumps(configs[name])},
                }),
                "stderr": "",
            }
        if arguments[0] == "get" and arguments[1] == "configmaps":
            name = arguments[2]
            if name in applied:
                return {"available": True, "stdout": json.dumps(applied[name]), "stderr": ""}
            return {"available": False, "stdout": "", "stderr": "NotFound"}
        if arguments[0] == "get" and arguments[1] == "baremetalhosts.metal3.io":
            hosts = [
                {
                    "spec": {"online": True, "consumerRef": None},
                    "status": {
                        "provisioning": {"state": "available"},
                        "operationalStatus": "OK",
                    },
                }
                for _ in range(available_bmhs)
            ]
            return {"available": True, "stdout": json.dumps({"items": hosts}), "stderr": ""}
        if arguments[0] == "get" and arguments[1] in (
            "machinedeployments",
            "kubeadmconfigtemplates",
            "metal3machinetemplates",
            "metal3datatemplates",
        ):
            name = arguments[2]
            if name in applied:
                return {"available": True, "stdout": json.dumps(applied[name]), "stderr": ""}
            if name == "workers" and existing:
                return {"available": True, "stdout": json.dumps(existing), "stderr": ""}
            return {"available": False, "stdout": "", "stderr": "NotFound"}
        if arguments[0] == "apply":
            path = arguments[-1]
            with open(path, encoding="utf-8") as stream:
                docs = list(yaml.safe_load_all(stream))
            if "--dry-run=server" not in arguments:
                for doc in docs:
                    if doc["kind"] == "MachineDeployment":
                        doc["metadata"]["generation"] = 1
                        doc["status"] = {
                            "observedGeneration": 1,
                            "replicas": 2,
                            "updatedReplicas": updated_replicas,
                        }
                    applied[doc["metadata"]["name"]] = doc
            return {"available": True, "stdout": "applied", "stderr": ""}
        raise AssertionError(arguments)
    return runner, calls, applied


def _payload():
    return {
        "target_namespace": "metal3",
        "target_cluster_name": "target",
        "deployment_id": "deployment-1",
        "worker_resources": _resources(),
        "worker_network_contract": _network_contract(),
        "sriov_device_plugin": {
            "pool4": {
                "node_selectors": {"type": "high-throughput"},
                "tolerations": [{
                    "effect": "NoSchedule",
                    "key": "high-throughput",
                    "operator": "Equal",
                    "value": "true",
                }],
                "sriov_network_device_plugin_configmap_name": "sriov-device-plugin-pool4",
                "sriov_network_device_plugin_configmap_data": {
                    "resourceList": [{
                        "resourceName": "sriov_dpdk",
                        "resourcePrefix": "intel.com",
                        "isRdma": False,
                        "selectors": {
                            "vendors": ["8086"],
                            "drivers": ["vfio-pci"],
                            "rootDevices": ["0000:86:00.0"],
                            "isRdma": False,
                        },
                    }],
                },
            },
        },
        "ironic_internal_ntp_ip": "10.0.0.10",
    }


def test_worker_provisioning_discovers_crds_dry_runs_and_applies_all_resources():
    runner, calls, applied = _runner_factory()
    result = execute_worker_provisioning(
        _payload(),
        _proof(),
        runner,
        environ={"IRONIC_INTERNAL_NTP_IP": "10.0.0.10"},
    )
    assert result["worker_provisioning_resources_applied"] is True
    assert result["worker_provisioning_server_dry_run_passed"] is True
    assert result["worker_provisioning_controller_observed"] is True
    assert result["desired_worker_count"] == 2
    assert result["preprovision_bmh_capacity_verified"] is True
    assert result["preprovision_available_bmh_count"] == 2
    assert result["controller_updated_replicas"] == 2
    assert result["worker_ip_pools_verified"] == ["workers-data"]
    assert set(applied) == {
        "workers",
        "workers-bootstrap",
        "workers-infra",
        "workers-network",
        "sriov-device-plugin-pool4",
    }
    bootstrap = applied["workers-bootstrap"]
    script = "\n".join(bootstrap["spec"]["template"]["spec"]["preKubeadmCommands"])
    assert "server 10.0.0.10 iburst prefer" in script
    assert "pool " not in script
    assert sum(1 for call in calls if call[0] == "apply") == 6


def test_worker_provisioning_does_not_treat_observed_generation_as_reconciled_replicas(
    monkeypatch,
):
    monkeypatch.setattr("apps.target_agent.worker_provisioning.time.sleep", lambda _seconds: None)
    runner, _calls, _applied = _runner_factory(updated_replicas=0)
    try:
        execute_worker_provisioning(
            _payload(),
            _proof(),
            runner,
            environ={"IRONIC_INTERNAL_NTP_IP": "10.0.0.10"},
        )
    except WorkerProvisioningError as exc:
        assert str(exc) == "WORKER_PROVISIONING_MACHINE_DEPLOYMENT_REPLICAS_NOT_RECONCILED"
        assert exc.side_effects is True
    else:
        raise AssertionError(
            "Observed generation without updated replicas must not report provisioning"
        )


def test_worker_provisioning_blocks_when_free_bmh_capacity_is_insufficient():
    runner, calls, _applied = _runner_factory(available_bmhs=1)
    try:
        execute_worker_provisioning(
            _payload(),
            _proof(),
            runner,
            environ={"IRONIC_INTERNAL_NTP_IP": "10.0.0.10"},
        )
    except WorkerProvisioningError as exc:
        assert str(exc) == "WORKER_PROVISIONING_AVAILABLE_BMH_CAPACITY_INSUFFICIENT"
    else:
        raise AssertionError("Insufficient free BMH capacity must block provisioning")
    assert not any(call[0] == "apply" for call in calls)


def test_worker_provisioning_blocks_when_required_network_attachment_is_missing():
    runner, calls, _applied = _runner_factory(missing_attachments={"data-sriov"})
    try:
        execute_worker_provisioning(
            _payload(),
            _proof(),
            runner,
            environ={"IRONIC_INTERNAL_NTP_IP": "10.0.0.10"},
        )
    except WorkerProvisioningError as exc:
        assert str(exc) == "WORKER_PROVISIONING_NETWORK_ATTACHMENT_NOT_FOUND"
    else:
        raise AssertionError("Missing SR-IOV NetworkAttachmentDefinition must block workers")
    assert not any(call[0] == "apply" for call in calls)


def test_worker_provisioning_requires_sriov_nad_resource_in_device_plugin_config():
    runner, calls, _applied = _runner_factory()
    payload = _payload()
    payload["sriov_device_plugin"]["pool4"]["sriov_network_device_plugin_configmap_data"][
        "resourceList"
    ][0]["resourceName"] = "sriov_other"
    try:
        execute_worker_provisioning(
            payload,
            _proof(),
            runner,
            environ={"IRONIC_INTERNAL_NTP_IP": "10.0.0.10"},
        )
    except WorkerProvisioningError as exc:
        assert str(exc) == "WORKER_PROVISIONING_SRIOV_RESOURCE_NOT_CONFIGURED"
    else:
        raise AssertionError("An unexposed SR-IOV resource must block worker provisioning")
    assert not any(call[0] == "apply" for call in calls)


def test_worker_provisioning_blocks_control_and_data_network_overlap():
    runner, calls, _applied = _runner_factory()
    payload = _payload()
    payload["worker_network_contract"]["data_plane_cidr"] = "10.0.0.128/25"
    try:
        execute_worker_provisioning(
            payload,
            _proof(),
            runner,
            environ={"IRONIC_INTERNAL_NTP_IP": "10.0.0.10"},
        )
    except WorkerProvisioningError as exc:
        assert str(exc) == "WORKER_PROVISIONING_CONTROL_DATA_NETWORK_OVERLAP"
    else:
        raise AssertionError("Overlapping control and data networks must block provisioning")
    assert not any(call[0] == "apply" for call in calls)


def test_worker_provisioning_blocks_scale_down_without_drain_executor():
    existing = {
        "metadata": {
            "name": "workers",
            "labels": {
                "ai-agent-stack.io/managed-by": "target-agent",
                "ai-agent-stack.io/deployment-id": "deployment-1",
            },
            "annotations": {"ai-agent-stack.io/target-cluster-uid": "target-uid"},
        },
        "spec": {"replicas": 3},
    }
    runner, calls, _applied = _runner_factory(existing_machine_deployment=existing)
    payload = _payload()
    payload["worker_resources"][0]["spec"]["replicas"] = 2
    try:
        execute_worker_provisioning(
            payload,
            _proof(),
            runner,
            environ={"IRONIC_INTERNAL_NTP_IP": "10.0.0.10"},
        )
    except WorkerProvisioningError as exc:
        assert str(exc) == "WORKER_PROVISIONING_SCALE_DOWN_REQUIRES_DRAIN_EXECUTOR"
    else:
        raise AssertionError("Scale-down must require a dedicated drain executor")
    assert not any(call[0] == "apply" for call in calls)


def test_worker_provisioning_rejects_external_ntp_pool_in_worker_bootstrap():
    runner, calls, _applied = _runner_factory()
    payload = _payload()
    script = render_worker_chrony_pre_kubeadm_command("10.0.0.10")
    payload["worker_resources"][1]["spec"]["template"]["spec"]["preKubeadmCommands"] = [
        script + "\npool pool.ntp.org iburst"
    ]
    try:
        execute_worker_provisioning(
            payload,
            _proof(),
            runner,
            environ={"IRONIC_INTERNAL_NTP_IP": "10.0.0.10"},
        )
    except WorkerProvisioningError as exc:
        assert str(exc) == "WORKER_PROVISIONING_WORKER_NTP_SCRIPT_INVALID"
    else:
        raise AssertionError("Worker external NTP pools must be rejected")
    assert not any(call[0] == "apply" for call in calls)



def test_worker_provisioning_preserves_partial_apply_state_on_failure():
    runner, _calls, _applied = _runner_factory()
    actual_applies = 0

    def fail_during_apply(arguments, **kwargs):
        nonlocal actual_applies
        if arguments[0] == "apply" and "--dry-run=server" not in arguments:
            actual_applies += 1
            if actual_applies == 3:
                return {"available": False, "stdout": "", "stderr": "simulated API timeout"}
        return runner(arguments, **kwargs)

    try:
        execute_worker_provisioning(
            _payload(),
            _proof(),
            fail_during_apply,
            environ={"IRONIC_INTERNAL_NTP_IP": "10.0.0.10"},
        )
    except WorkerProvisioningError as exc:
        assert exc.side_effects is True
    else:
        raise AssertionError("A partial apply failure must retain side-effect state")


def test_worker_provisioning_action_requires_completed_eph_cleanup_checkpoint(monkeypatch):
    monkeypatch.setattr(
        target_main,
        "_execute_worker_provisioning",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("worker provisioning must not run before EPH cleanup")
        ),
    )
    outcome = target_main.execute({
        "action": "target.worker.provision",
        "payload": {},
        "deployment_checkpoint": {"phase": "bmh_registry_migrated"},
    })
    assert outcome["status"] == "blocked"
    assert outcome["error_code"] == "WORKER_PROVISIONING_EPH_CLEANUP_CHECKPOINT_REQUIRED"



def test_worker_provisioning_rejects_swapped_control_and_data_network_bindings():
    runner, calls, _applied = _runner_factory()
    payload = _payload()
    network_data = payload["worker_resources"][3]["spec"]["networkData"]["networks"]
    network_data["ipv4DHCP"][0]["link"] = "ens1f0"
    network_data["ipv4"][0]["link"] = "eno1"

    try:
        execute_worker_provisioning(
            payload,
            _proof(),
            runner,
            environ={"IRONIC_INTERNAL_NTP_IP": "10.0.0.10"},
        )
    except WorkerProvisioningError as exc:
        assert str(exc) == "WORKER_PROVISIONING_DATA_INTERFACE_IP_POOL_REQUIRED"
    else:
        raise AssertionError("The data-plane interface must be backed by its IP pool")

    assert not any(call[0] == "apply" for call in calls)


def test_worker_provisioning_rejects_one_interface_bound_to_multiple_networks():
    runner, calls, _applied = _runner_factory()
    payload = _payload()
    network_data = payload["worker_resources"][3]["spec"]["networkData"]["networks"]
    network_data["ipv4"][0]["link"] = "eno1"

    try:
        execute_worker_provisioning(
            payload,
            _proof(),
            runner,
            environ={"IRONIC_INTERNAL_NTP_IP": "10.0.0.10"},
        )
    except WorkerProvisioningError as exc:
        assert str(exc) == "WORKER_PROVISIONING_NETWORK_INTERFACE_MAPPED_MULTIPLE_TIMES"
    else:
        raise AssertionError("An interface must not be attached to multiple network definitions")

    assert not any(call[0] == "apply" for call in calls)



def test_worker_provisioning_requires_trusted_eph_cleanup_scope_proof(monkeypatch):
    monkeypatch.setattr(
        target_main,
        "_observe_target_metal3_ownership",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Metal3 observation must not run without cleanup proof")
        ),
    )
    try:
        target_main._execute_worker_provisioning(
            _payload(),
            {"eph_pivot_attestation": _proof()["eph_pivot_attestation"]},
        )
    except target_main.HandoffError as exc:
        assert str(exc) == "WORKER_PROVISIONING_CLEANUP_SCOPE_ATTESTATION_REQUIRED"
    else:
        raise AssertionError("Worker provisioning must require trusted cleanup scope proof")



def test_lifecycle_command_payload_binds_cloud_deployment_identity():
    payload = target_main._command_payload({
        "deployment_id": "deployment-1",
        "payload": {"target_namespace": "metal3"},
    })
    assert payload["deployment_id"] == "deployment-1"
    try:
        target_main._command_payload({
            "deployment_id": "deployment-1",
            "payload": {"deployment_id": "deployment-2"},
        })
    except target_main.HandoffError as exc:
        assert str(exc) == "AGENT_COMMAND_DEPLOYMENT_ID_MISMATCH"
    else:
        raise AssertionError("Lifecycle command must reject a mismatched deployment ID")


def test_worker_provisioning_applies_sriov_configmap_from_selected_pci_resources():
    runner, calls, applied = _runner_factory()
    payload = _payload()
    pool = {
        "node_selectors": {"type": "high-throughput"},
        "tolerations": [
            {
                "effect": "NoSchedule",
                "key": "high-throughput",
                "operator": "Equal",
                "value": "true",
            }
        ],
        "sriov_network_device_plugin_configmap_name": "sriov-dp-configmap",
        "sriov_network_device_plugin_configmap_data": {
            "resourceList": [
                {
                    "resourceName": "mlnx_dpdk_left",
                    "resourcePrefix": "mellanox.com",
                    "isRdma": True,
                    "selectors": {
                        "vendors": ["15b3"],
                        "drivers": ["mlx5_core"],
                        "rootDevices": ["0000:2f:00.0", "0000:8a:00.0"],
                        "isRdma": True,
                    },
                }
            ]
        },
    }
    payload["sriov_device_plugin"] = {"pool4": pool, "pool5": pool}
    result = execute_worker_provisioning(
        payload,
        _proof(),
        runner,
        environ={"IRONIC_INTERNAL_NTP_IP": "10.0.0.10"},
    )
    configmap = applied["sriov-dp-configmap"]
    assert configmap["kind"] == "ConfigMap"
    assert (
        configmap["metadata"]["annotations"]["ai-agent-stack.io/target-cluster-uid"]
        == "target-uid"
    )
    config = json.loads(configmap["data"]["config.json"])
    assert config["resourceList"][0]["selectors"]["rootDevices"] == [
        "0000:2f:00.0",
        "0000:8a:00.0",
    ]
    assert result["worker_provisioning_resources_applied"] is True
    assert sum(1 for call in calls if call[0] == "apply") == 6


def test_sriov_configmap_ownership_accepts_kubernetes_not_found_variants():
    configmaps = [{
        "metadata": {
            "name": "sriov-dp-configmap",
            "annotations": {"ai-agent-stack.io/target-cluster-uid": "target-uid"},
        },
    }]
    for stderr in (
        'Error from server (NotFound): configmaps "sriov-dp-configmap" not found',
        "NotFound",
        'configmaps "sriov-dp-configmap" not found',
    ):
        _verify_sriov_configmap_ownership(
            lambda *_args, _stderr=stderr, **_kwargs: {
                "available": False,
                "stdout": "",
                "stderr": _stderr,
            },
            configmaps,
            "metal3",
            "deployment-1",
            "target",
        )


def test_sriov_configmap_ownership_does_not_hide_api_or_transport_errors():
    configmaps = [{
        "metadata": {
            "name": "sriov-dp-configmap",
            "annotations": {"ai-agent-stack.io/target-cluster-uid": "target-uid"},
        },
    }]
    for stderr in ("connection refused", "Unauthorized", "TLS handshake timeout"):
        try:
            _verify_sriov_configmap_ownership(
                lambda *_args, _stderr=stderr, **_kwargs: {
                    "available": False,
                    "stdout": "",
                    "stderr": _stderr,
                },
                configmaps,
                "metal3",
                "deployment-1",
                "target",
            )
        except WorkerProvisioningError as exc:
            assert str(exc) == "SRIOV_DEVICE_PLUGIN_CONFIGMAP_LOOKUP_FAILED"
        else:
            raise AssertionError(f"Unexpectedly ignored ConfigMap lookup error: {stderr}")
