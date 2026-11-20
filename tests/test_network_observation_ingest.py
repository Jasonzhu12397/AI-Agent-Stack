from core.agent_node_views import _network_observation


def test_merges_os_network_fields_without_dropping_identity():
    inventory = {
        "status": "observed",
        "system_identity": {"sys_vendor": "HPE"},
        "nics": [{"name": "old"}],
    }
    result = {
        "status": "succeeded",
        "output": {
            "nics": [{"name": "eno1", "mtu": 1500}],
            "bonds": [{"name": "bond0", "interfaces": ["eno1"]}],
        },
    }
    merged = _network_observation(inventory, result)
    assert merged["system_identity"] == {"sys_vendor": "HPE"}
    assert merged["nics"] == [{"name": "eno1", "mtu": 1500}]
    assert merged["bonds"][0]["name"] == "bond0"
    assert merged["network_observation"] == {
        "source": "host.network.observe",
        "nics": 1,
        "bonds": 1,
    }


def test_rejects_non_list_observation():
    assert _network_observation({"status": "observed"}, {"output": {"nics": "bad"}}) is None
    assert _network_observation({}, {}) is None
