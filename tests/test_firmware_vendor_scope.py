from __future__ import annotations

from types import SimpleNamespace

import pytest
from core.agent_node_views import _firmware_vendor_scope


@pytest.mark.parametrize(
    "vendor,product,expected",
    [
        ("HPE", "ProLiant DL380 Gen10", "HPE"),
        ("Hewlett Packard Enterprise", "ProLiant", "HPE"),
        ("Dell Inc.", "PowerEdge R750", "Dell"),
        ("Inspur", "NF5280M6", "Inspur"),
        ("浪潮", "NF5280M6", "Inspur"),
        ("H3C", "H3C UniServer R4900 G5", "H3C"),
        ("New H3C Technologies", "UniServer", "H3C"),
        ("Lenovo", "ThinkSystem SR650", "Lenovo"),
        ("联想", "ThinkSystem", "Lenovo"),
        ("Supermicro", "SYS-1029U", None),
        ("", "", None),
    ],
)
def test_firmware_vendor_scope(vendor, product, expected) -> None:
    machine = SimpleNamespace(vendor=vendor, product_name=product)
    assert _firmware_vendor_scope(machine) == expected
