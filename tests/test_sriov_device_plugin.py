import pytest
from apps.agent_contracts.sriov_device_plugin import (
    SriovDevicePluginConfigError,
    render_sriov_device_plugin_configmap,
    validate_sriov_device_plugin_config,
)


def _pool(root_devices, resource_name="mlnx_dpdk_left", configmap="sriov-dp-configmap"):
    return {
        "node_selectors": {"type": "high-throughput"},
        "tolerations": [
            {
                "effect": "NoSchedule",
                "key": "high-throughput",
                "operator": "Equal",
                "value": "true",
            }
        ],
        "sriov_network_device_plugin_configmap_name": configmap,
        "sriov_network_device_plugin_configmap_data": {
            "resourceList": [
                {
                    "resourceName": resource_name,
                    "resourcePrefix": "mellanox.com",
                    "isRdma": True,
                    "selectors": {
                        "vendors": ["15b3"],
                        "drivers": ["mlx5_core"],
                        "rootDevices": root_devices,
                        "isRdma": True,
                    },
                }
            ]
        },
    }


def test_validate_sriov_config_accepts_user_pci_bus_addresses_and_shared_configmap():
    config = {
        "pool4": _pool(["0000:2f:00.0", "0000:8a:00.0"]),
        "pool5": _pool(["0000:2f:00.0", "0000:8a:00.0"]),
    }
    normalized = validate_sriov_device_plugin_config(config)
    assert normalized["pool4"]["sriov_network_device_plugin_configmap_name"] == "sriov-dp-configmap"
    assert (
        normalized["pool5"]["sriov_network_device_plugin_configmap_data"]
        == normalized["pool4"]["sriov_network_device_plugin_configmap_data"]
    )


@pytest.mark.parametrize(
    "addresses",
    [
        ["2f:00.0"],
        ["0000:2f:00.8"],
        ["0000:2f:00.0", "0000:2f:00.0"],
        ["not-a-pci-address"],
    ],
)
def test_validate_sriov_config_rejects_invalid_or_duplicate_pci_addresses(addresses):
    with pytest.raises(SriovDevicePluginConfigError):
        validate_sriov_device_plugin_config({"pool4": _pool(addresses)})


def test_validate_sriov_config_rejects_same_configmap_with_conflicting_data():
    with pytest.raises(SriovDevicePluginConfigError, match="CONFIGMAP_CONTENT_CONFLICT"):
        validate_sriov_device_plugin_config({
            "pool4": _pool(["0000:2f:00.0"]),
            "pool5": _pool(["0000:8a:00.0"]),
        })


def test_validate_sriov_config_rejects_invalid_vendor_and_resource_name():
    config = {"pool4": _pool(["0000:2f:00.0"])}
    selectors = config["pool4"]["sriov_network_device_plugin_configmap_data"]
    selectors["resourceList"][0]["selectors"]["vendors"] = ["xyz"]
    with pytest.raises(SriovDevicePluginConfigError, match="SELECTORS_INVALID"):
        validate_sriov_device_plugin_config(config)


def test_render_sriov_configmap_adds_deployment_ownership():
    normalized = validate_sriov_device_plugin_config({"pool4": _pool(["0000:2f:00.0"])})
    configmap = render_sriov_device_plugin_configmap(
        normalized["pool4"],
        namespace="metal3",
        deployment_id="deployment-1",
        cluster_name="edge-lab",
        target_cluster_uid="target-uid",
    )
    assert configmap["kind"] == "ConfigMap"
    assert (
        configmap["metadata"]["labels"]["ai-agent-stack.io/deployment-id"]
        == "deployment-1"
    )
    assert (
        configmap["metadata"]["labels"]["cluster.x-k8s.io/cluster-name"] == "edge-lab"
    )
    assert (
        configmap["metadata"]["annotations"]["ai-agent-stack.io/target-cluster-uid"]
        == "target-uid"
    )
    assert '"rootDevices":["0000:2f:00.0"]' in configmap["data"]["config.json"]


def test_validate_sriov_config_rejects_overlapping_pools_with_different_configmaps():
    left = _pool(["0000:2f:00.0"], configmap="sriov-pool4")
    right = _pool(["0000:8a:00.0"], configmap="sriov-pool5")
    with pytest.raises(SriovDevicePluginConfigError, match="POOL_NODE_SELECTORS_OVERLAP"):
        validate_sriov_device_plugin_config({"pool4": left, "pool5": right})


def test_validate_sriov_config_allows_disjoint_node_selector_pools():
    left = _pool(["0000:2f:00.0"], configmap="sriov-pool4")
    right = _pool(["0000:8a:00.0"], configmap="sriov-pool5")
    right["node_selectors"] = {"type": "low-latency"}
    normalized = validate_sriov_device_plugin_config({"pool4": left, "pool5": right})
    assert set(normalized) == {"pool4", "pool5"}
