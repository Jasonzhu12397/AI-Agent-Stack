import pytest

from apps.target_agent.network_plugins import (
    NetworkStackError,
    data_plane_plugins,
    plan_network_stack,
)


def test_default_stack_enables_only_base_calico():
    planned = plan_network_stack({})
    by_name = {plugin.name: plugin for plugin in planned}
    assert by_name["calico"].enabled is True
    for name in ("multus", "ovs", "sriov", "dpdk"):
        assert by_name[name].enabled is False


def test_explicit_data_plane_plugins_are_enabled_with_version_and_config():
    planned = plan_network_stack(
        {
            "plugins": {
                "sriov": {"enabled": True, "version": "v2.7.0", "config": {"vf_alloc": 8}},
                "dpdk": {"enabled": True},
            }
        }
    )
    by_name = {plugin.name: plugin for plugin in planned}
    assert by_name["sriov"].enabled is True
    assert by_name["sriov"].version == "v2.7.0"
    assert by_name["sriov"].config == {"vf_alloc": 8}
    assert by_name["dpdk"].enabled is True
    assert by_name["dpdk"].version is None


def test_data_plane_plugin_set_matches_high_performance_plugins():
    assert data_plane_plugins() == {"ovs", "sriov", "dpdk"}


def test_rejects_unknown_plugin():
    with pytest.raises(NetworkStackError, match="NETWORK_STACK_PLUGINS_INVALID"):
        plan_network_stack({"plugins": {"weave": {"enabled": True}}})


def test_rejects_malformed_version():
    with pytest.raises(NetworkStackError, match="NETWORK_STACK_VERSION_INVALID"):
        plan_network_stack({"plugins": {"sriov": {"enabled": True, "version": "2.7"}}})


def test_rejects_non_boolean_enabled_flag():
    with pytest.raises(NetworkStackError, match="NETWORK_STACK_ENABLED_INVALID"):
        plan_network_stack({"plugins": {"sriov": {"enabled": "yes"}}})


def test_rejects_non_mapping_config():
    with pytest.raises(NetworkStackError, match="NETWORK_STACK_CONFIG_INVALID"):
        plan_network_stack({"plugins": {"sriov": {"enabled": True, "config": ["vf"]}}})


def test_rejects_non_mapping_spec():
    with pytest.raises(NetworkStackError, match="NETWORK_STACK_SPEC_INVALID"):
        plan_network_stack(["not", "a", "dict"])
