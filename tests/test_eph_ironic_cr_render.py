"""The EPH must render a real Ironic CR or refuse to continue."""

import pytest

from apps.eph_agent.ironic_cr import (
    IronicRenderError,
    contains_ironic_cr,
    render_ironic_cr,
)


def _spec(**overrides):
    spec = {
        "ironic": {
            "api_credentials_name": "ironic-auth",
            "version": "37.0",
            "images": {
                "ironic": "registry.local/ironic:37.0",
                "deploy_ramdisk_downloader": "registry.local/ipa-downloader:latest",
                "keepalived": "registry.local/keepalived:2.2.4",
            },
            "networking": {
                "interface": "eno2",
                "ip_address": "10.130.60.84",
                "image_server_port": 8080,
                "disable_host_network": True,
                "mac_addresses": ["00:11:22:33:44:55"],
            },
        }
    }
    spec["ironic"].update(overrides)
    return spec


def test_renderer_returns_the_operator_contract_shape():
    document = render_ironic_cr(_spec())
    assert document == {
        "apiVersion": "ironic.metal3.io/v1alpha1",
        "kind": "Ironic",
        "metadata": {"name": "ironic", "namespace": "baremetal-operator-system"},
        "spec": {
            "apiCredentialsName": "ironic-auth",
            "version": "37.0",
            "images": {
                "ironic": "registry.local/ironic:37.0",
                "deployRamdiskDownloader": "registry.local/ipa-downloader:latest",
                "keepalived": "registry.local/keepalived:2.2.4",
            },
            "networking": {
                "interface": "eno2",
                "ipAddress": "10.130.60.84",
                "imageServerPort": 8080,
                "disableHostNetwork": True,
                "macAddresses": ["00:11:22:33:44:55"],
            },
        },
    }


def test_missing_section_means_the_caller_supplies_the_cr():
    assert render_ironic_cr({}) is None
    assert render_ironic_cr({"cluster_name": "lab"}) is None


def test_renderer_rejects_incomplete_specs_instead_of_inventing_values():
    for overrides, code in (
        ({"api_credentials_name": None}, "IRONIC_CREDENTIALS_REQUIRED"),
        ({"version": " "}, "IRONIC_VERSION_REQUIRED"),
        ({"images": {"ironic": ""}}, "IRONIC_IMAGE_REQUIRED"),
        ({"networking": {"interface": "eno2"}}, "IRONIC_IP_ADDRESS_REQUIRED"),
        ({"networking": {"ip_address": "10.0.0.1"}}, "IRONIC_INTERFACE_REQUIRED"),
        ({"networking": []}, "IRONIC_NETWORKING_REQUIRED"),
        ({"networking": {"interface": "eno2", "ip_address": "10.0.0.1", "api_port": 0}},
         "IRONIC_NETWORKING_INVALID"),
        ({"namespace": "metal3"}, "IRONIC_NAMESPACE_UNSUPPORTED"),
    ):
        with pytest.raises(IronicRenderError) as excinfo:
            render_ironic_cr(_spec(**overrides))
        assert str(excinfo.value) == code


def test_renderer_keeps_optional_networking_values():
    document = render_ironic_cr(
        _spec(
            networking={
                "interface": "eno2",
                "ip_address": "10.130.60.84",
                "external_ip": "10.130.60.90",
                "ip_address_manager": "keepalived",
                "rpc_port": 6385,
                "bind_interface": False,
            }
        )
    )
    networking = document["spec"]["networking"]
    assert networking["externalIP"] == "10.130.60.90"
    assert networking["ipAddressManager"] == "keepalived"
    assert networking["rpcPort"] == 6385
    assert networking["bindInterface"] is False


def test_manifest_cr_detection():
    assert contains_ironic_cr([]) is False
    assert contains_ironic_cr(None) is False
    assert contains_ironic_cr(
        [{"apiVersion": "ironic.metal3.io/v1alpha1", "kind": "Ironic"}]
    ) is True
    assert contains_ironic_cr([{"apiVersion": "apps/v1", "kind": "Ironic"}]) is False
