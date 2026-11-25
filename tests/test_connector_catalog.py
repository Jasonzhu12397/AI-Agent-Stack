import json
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parents[1] / "deploy" / "agent-plugins" / "catalog.json"


def load_catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def test_connector_catalog_has_unique_keys():
    catalog = load_catalog()
    keys = [plugin["plugin_key"] for plugin in catalog["plugins"]]

    assert len(keys) == len(set(keys))


def test_expected_mainland_connectors_are_catalogued():
    plugins = {plugin["plugin_key"]: plugin for plugin in load_catalog()["plugins"]}
    expected = {
        "amap": "AMAP_CONNECTOR_URL",
        "baidu-map": "BAIDU_MAP_CONNECTOR_URL",
        "rail-12306": "RAIL_12306_CONNECTOR_URL",
        "didi": "DIDI_CONNECTOR_URL",
        "china-services": "CHINA_SERVICES_CONNECTOR_URL",
        "wechat": "WECHAT_CONNECTOR_URL",
    }

    for key, endpoint_env in expected.items():
        plugin = plugins[key]
        assert plugin["endpoint_env"] == endpoint_env
        assert plugin["auth_required"] is True
        assert plugin["capabilities"]
        assert all(capability["input_schema"]["required"] for capability in plugin["capabilities"])


def test_wechat_connector_documents_the_official_api_boundary():
    plugins = {plugin["plugin_key"]: plugin for plugin in load_catalog()["plugins"]}
    description = plugins["wechat"]["description"]

    assert "Official Account" in description
    assert "Personal WeChat" in description
