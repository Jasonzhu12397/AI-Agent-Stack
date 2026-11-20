"""The Ironic contract must be one rule set shared by every caller."""

import pytest

from packages.agent_contracts.ironic_spec import (
    API_VERSION,
    CR_NAMESPACE,
    CRD_NAME,
    IronicSpecError,
    normalize_ironic_config,
)


def _config(**overrides):
    config = {
        "api_credentials_name": "ironic-auth",
        "version": "37.0",
        "images": {
            "ironic": "registry.local/ironic:37.0",
            "deploy_ramdisk_downloader": "registry.local/ipa-downloader:latest",
            "keepalived": "registry.local/keepalived:2.2.4",
        },
        "networking": {"interface": "eno2", "ip_address": "10.130.60.84"},
    }
    config.update(overrides)
    return config


def test_constants_are_the_ones_the_probe_and_ci_rely_on():
    assert API_VERSION == "ironic.metal3.io/v1alpha1"
    assert CR_NAMESPACE == "baremetal-operator-system"
    assert CRD_NAME == "ironics.ironic.metal3.io"


def test_normalizer_returns_only_camel_case_spec_fields():
    assert normalize_ironic_config(_config()) == {
        "apiCredentialsName": "ironic-auth",
        "version": "37.0",
        "images": {
            "ironic": "registry.local/ironic:37.0",
            "deployRamdiskDownloader": "registry.local/ipa-downloader:latest",
            "keepalived": "registry.local/keepalived:2.2.4",
        },
        "networking": {"interface": "eno2", "ipAddress": "10.130.60.84"},
    }


def test_normalizer_rejects_unknown_types_and_out_of_range_values():
    cases = [
        (["not", "a", "mapping"], "IRONIC_SPEC_INVALID"),
        (_config(namespace="metal3"), "IRONIC_NAMESPACE_UNSUPPORTED"),
        (_config(images=[]), "IRONIC_IMAGES_REQUIRED"),
        (_config(networking=[]), "IRONIC_NETWORKING_REQUIRED"),
        (
            _config(
                networking={"interface": "eno2", "ip_address": "10.0.0.1", "api_port": True}
            ),
            "IRONIC_NETWORKING_INVALID",
        ),
        (
            _config(
                networking={"interface": "eno2", "ip_address": "10.0.0.1", "mac_addresses": [1]}
            ),
            "IRONIC_NETWORKING_INVALID",
        ),
    ]
    for config, code in cases:
        with pytest.raises(IronicSpecError) as excinfo:
            normalize_ironic_config(config)
        assert str(excinfo.value) == code


def test_normalizer_keeps_passthrough_networking_values():
    config = _config(
        networking={
            "interface": "eno2",
            "ip_address": "10.130.60.84",
            "external_ip": "10.130.60.90",
            "image_server_port": 8080,
            "disable_host_network": True,
            "mac_addresses": ["00:11:22:33:44:55"],
        }
    )
    networking = normalize_ironic_config(config)["networking"]
    assert networking["externalIP"] == "10.130.60.90"
    assert networking["imageServerPort"] == 8080
    assert networking["disableHostNetwork"] is True
    assert networking["macAddresses"] == ["00:11:22:33:44:55"]


def test_importing_the_contract_does_not_pull_in_pydantic():
    """The Metal3 CRD CI job installs only PyYAML and must stay able to import it."""

    import subprocess
    import sys

    script = (
        "import sys;"
        "import packages.agent_contracts.ironic_spec as spec;"
        "assert 'pydantic' not in sys.modules, 'pydantic was imported eagerly';"
        "print(spec.CRD_NAME)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ironics.ironic.metal3.io"


def test_package_exports_still_resolve_lazily():
    from packages.agent_contracts import AgentRole, plan_next_step

    assert AgentRole is not None
    assert callable(plan_next_step)
