"""Contract tests for the EPH Live ISO bootstrap stage.

The bootstrap lives outside the Python package tree, so it is loaded by path.
Two invariants matter most: the seed configuration is validated before any
cluster write happens, and the Deployment handed to ``kubectl apply`` always
carries the environment ``AgentSettings`` requires.
"""

import importlib.util
import pathlib
import shutil
import subprocess

import pytest
import yaml

from apps.eph_agent.bootstrap_renderer import render_agent_deployment

BOOTSTRAP_PATH = pathlib.Path(__file__).resolve().parents[1] / "eph-iso" / "bootstrap.py"
AGENT_ID = "0b8a5dc2-6f6f-4b4e-9a4c-3f6a1b7c8d90"
INSTALLATION_ID = "1f0c2a44-8b1d-4c3e-8f5a-2b6c9d0e1f22"
PINNED_IMAGE = "registry.example.test/eph-agent@sha256:" + "a" * 64
PLACEHOLDER = "REPLACE_WITH_REGISTRY/ai-agent-node-agent:VERSION"
PLACEHOLDER_INVALID = "EPH_AGENT_IMAGE_PLACEHOLDER_INVALID"
DIGEST_INVALID = "AGENT_IMAGE_NOT_PINNED_BY_DIGEST"


@pytest.fixture(scope="module")
def bootstrap_module():
    spec = importlib.util.spec_from_file_location("eph_iso_bootstrap", BOOTSTRAP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(**overrides: object) -> dict:
    config = {
        "control_url": "https://cloud-control.example.test",
        "agent_id": AGENT_ID,
        "installation_id": INSTALLATION_ID,
        "agent_image": PINNED_IMAGE,
    }
    config.update(overrides)
    return config


def test_valid_config_normalizes_the_control_url(bootstrap_module):
    config = bootstrap_module.validate_config(_config(control_url="https://cp.example.test/"))
    assert config["control_url"] == "https://cp.example.test"
    assert config["agent_id"] == AGENT_ID
    assert config["instance_id"] is None


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"control_url": "http://cp.example.test"}, "CONTROL_URL_INVALID"),
        ({"control_url": "https://cp.example.test/api"}, "CONTROL_URL_INVALID"),
        ({"agent_id": "eph-bootstrap-01"}, "AGENT_ID_INVALID"),
        ({"installation_id": "not-a-uuid"}, "INSTALLATION_ID_INVALID"),
        ({"instance_id": ""}, "INSTANCE_ID_INVALID"),
        ({"instance_id": "x" * 129}, "INSTANCE_ID_INVALID"),
        ({"agent_image": "registry.example.test/eph-agent:v1"}, DIGEST_INVALID),
    ],
)
def test_config_rejects_invalid_fields(bootstrap_module, overrides, code):
    with pytest.raises(bootstrap_module.BootstrapError, match=code):
        bootstrap_module.validate_config(_config(**overrides))


def test_config_requires_all_mandatory_fields(bootstrap_module):
    for field in ("control_url", "agent_id", "installation_id", "agent_image"):
        document = _config()
        document.pop(field)
        with pytest.raises(
            bootstrap_module.BootstrapError, match="BOOTSTRAP_CONFIG_FIELDS_MISSING"
        ):
            bootstrap_module.validate_config(document)


def test_config_rejects_non_mapping_documents(bootstrap_module):
    with pytest.raises(bootstrap_module.BootstrapError, match="BOOTSTRAP_CONFIG_INVALID"):
        bootstrap_module.validate_config(["not", "a", "mapping"])


def _deployment_yaml_with_placeholder() -> str:
    spec = {
        "node_name": "eph-bootstrap-01",
        "kubernetes_version": "v1.31.2",
        "node_ip": "192.0.2.10",
        "pod_cidr": "10.244.0.0/16",
        "service_cidr": "10.96.0.0/12",
        "control_plane_endpoint": "192.0.2.10",
        "agent": {
            "control_url": "https://cloud-control.example.test",
            "image": "oci://registry.example.test/eph-agent@" + "sha256:" + "b" * 64,
            "mtls_secret_ref": "eph-agent-mtls",
            "agent_id": AGENT_ID,
            "installation_id": INSTALLATION_ID,
        },
    }
    deployment = render_agent_deployment(spec)
    document = yaml.safe_dump(deployment, sort_keys=False)
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return document.replace(container["image"], PLACEHOLDER)


def test_deployment_from_the_renderer_carries_every_agent_variable(bootstrap_module):
    document = _deployment_yaml_with_placeholder()
    rendered = bootstrap_module.validate_deployment_manifest(document, PINNED_IMAGE)
    assert PLACEHOLDER not in rendered
    assert PINNED_IMAGE in rendered
    for name in bootstrap_module.REQUIRED_AGENT_ENVIRONMENT:
        assert name in rendered


def test_required_environment_matches_the_transport_contract(bootstrap_module):
    from packages.agent_transport import config as transport_config

    text = pathlib.Path(transport_config.__file__).read_text(encoding="utf-8")
    for name in bootstrap_module.REQUIRED_AGENT_ENVIRONMENT:
        assert name in text
    assert set(bootstrap_module.REQUIRED_AGENT_ENVIRONMENT) == {
        "AGENT_CONTROL_URL",
        "AGENT_ROLE",
        "AGENT_ID",
        "AGENT_INSTALLATION_ID",
        "AGENT_INSTANCE_ID",
        "AGENT_TLS_CA_FILE",
        "AGENT_TLS_CERT_FILE",
        "AGENT_TLS_KEY_FILE",
    }


def test_deployment_without_the_placeholder_is_rejected(bootstrap_module):
    with pytest.raises(bootstrap_module.BootstrapError, match=PLACEHOLDER_INVALID):
        bootstrap_module.validate_deployment_manifest("kind: Deployment\n", PINNED_IMAGE)


def test_deployment_with_duplicate_placeholder_is_rejected(bootstrap_module):
    document = _deployment_yaml_with_placeholder()
    with pytest.raises(bootstrap_module.BootstrapError, match=PLACEHOLDER_INVALID):
        bootstrap_module.validate_deployment_manifest(
            document + "\n#" + PLACEHOLDER + "\n", PINNED_IMAGE
        )


def test_deployment_missing_agent_environment_is_rejected(bootstrap_module):
    document = _deployment_yaml_with_placeholder()
    document = document.replace("AGENT_INSTALLATION_ID", "LEGACY_ID")
    document = document.replace("AGENT_TLS_KEY_FILE", "LEGACY_KEY_FILE")
    with pytest.raises(
        bootstrap_module.BootstrapError,
        match="EPH_AGENT_DEPLOYMENT_ENVIRONMENT_INCOMPLETE:AGENT_INSTALLATION_ID,AGENT_TLS_KEY_FILE",
    ):
        bootstrap_module.validate_deployment_manifest(document, PINNED_IMAGE)


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")
def test_keypair_verification_accepts_a_matching_certificate(tmp_path, bootstrap_module):
    ca_key, ca_crt = _issue_ca(tmp_path)
    cert, key = _issue_leaf(tmp_path, ca_key, ca_crt)
    fingerprint = bootstrap_module.verify_keypair(cert, ca_crt, key)
    assert len(fingerprint) == 64


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")
def test_keypair_verification_rejects_a_foreign_key(tmp_path, bootstrap_module):
    ca_key, ca_crt = _issue_ca(tmp_path)
    cert, _ = _issue_leaf(tmp_path, ca_key, ca_crt, name="leaf-one")
    _, other_key = _issue_leaf(tmp_path, ca_key, ca_crt, name="leaf-two")
    with pytest.raises(bootstrap_module.BootstrapError, match="CLIENT_CERTIFICATE_KEY_MISMATCH"):
        bootstrap_module.verify_keypair(cert, ca_crt, other_key)


def _issue_ca(directory: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    key = directory / "ca.key"
    crt = directory / "ca.crt"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
         "-nodes", "-keyout", str(key), "-out", str(crt), "-days", "1", "-subj", "/CN=test-ca",
         "-addext", "basicConstraints=critical,CA:TRUE",
         "-addext", "keyUsage=critical,keyCertSign,cRLSign"],
        check=True, capture_output=True, text=True,
    )
    return key, crt


def _issue_leaf(
    directory: pathlib.Path,
    ca_key: pathlib.Path,
    ca_crt: pathlib.Path,
    name: str = "leaf",
) -> tuple[pathlib.Path, pathlib.Path]:
    key = directory / (name + ".key")
    csr = directory / (name + ".csr")
    crt = directory / (name + ".crt")
    subprocess.run(
        ["openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
         "-nodes", "-keyout", str(key), "-out", str(csr), "-subj", "/CN=" + name],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca_crt), "-CAkey", str(ca_key),
         "-CAcreateserial", "-out", str(crt), "-days", "1",
         "-extfile", "/dev/stdin"],
        input="extendedKeyUsage=clientAuth\nbasicConstraints=CA:FALSE\n",
        check=True, capture_output=True, text=True,
    )
    return crt, key
