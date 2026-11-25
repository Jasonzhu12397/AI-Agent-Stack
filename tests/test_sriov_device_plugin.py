from pathlib import Path

import pytest
from agent_contracts.sriov_device_plugin import (
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


def test_validate_sriov_config_accepts_yaml_block_scalar_tolerations():
    pool = _pool(["0000:2f:00.0"])
    pool["tolerations"] = [
        """
        {
          effect: NoSchedule,
          key: high-throughput,
          operator: Equal,
          value: "true"
        }
        """
    ]
    normalized = validate_sriov_device_plugin_config({"pool4": pool})
    assert normalized["pool4"]["node_selectors"] == {"type": "high-throughput"}
    assert normalized["pool4"]["tolerations"] == [
        {
            "effect": "NoSchedule",
            "key": "high-throughput",
            "operator": "Equal",
            "value": "true",
        }
    ]


def test_validate_sriov_config_accepts_yaml_string_for_device_plugin_config():
    import json

    pool = _pool(["0000:2f:00.0"])
    pool["sriov_network_device_plugin_configmap_data"] = json.dumps(
        pool["sriov_network_device_plugin_configmap_data"]
    )
    normalized = validate_sriov_device_plugin_config({"pool4": pool})
    assert normalized["pool4"]["sriov_network_device_plugin_configmap_data"][
        "resourceList"
    ][0]["selectors"]["rootDevices"] == ["0000:2f:00.0"]


def test_sriov_editor_emits_immutable_updates_and_uses_unique_defaults():
    editor = (
        Path(__file__).resolve().parents[1]
        / "apps/web-studio/src/components/SriovDevicePluginEditor.vue"
    ).read_text(encoding="utf-8")
    assert "v-model=" not in editor
    assert "@update:model-value=" in editor
    assert 'sriov-dp-configmap-" + name' in editor
    assert 'existingNames.has("mlnx_dpdk_" + suffix)' in editor


def test_validate_sriov_config_accepts_operator_chosen_pool_names():
    normalized = validate_sriov_device_plugin_config(
        {
            "pool-dpdk": _pool(["0000:2f:00.0"], configmap="sriov-pool-dpdk"),
            "gpu-numa0": {
                **_pool(["0000:8a:00.0"], configmap="sriov-pool-gpu"),
                "node_selectors": {"type": "gpu"},
            },
        }
    )

    assert set(normalized) == {"pool-dpdk", "gpu-numa0"}


@pytest.mark.parametrize("name", ["Pool4", "pool_4", "-pool", "pool-", ""])
def test_validate_sriov_config_rejects_non_kubernetes_pool_names(name):
    with pytest.raises(SriovDevicePluginConfigError, match="POOL_NAME_INVALID"):
        validate_sriov_device_plugin_config(
            {name: _pool(["0000:2f:00.0"], configmap="sriov-pool")}
        )
