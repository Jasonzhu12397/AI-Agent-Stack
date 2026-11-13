# ruff: noqa: I001
import base64
import importlib.util
import json
import pathlib
import uuid

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = ROOT / "eph-iso" / "bootstrap.py"
SPEC = importlib.util.spec_from_file_location("eph_bootstrap", BOOTSTRAP_PATH)
BOOTSTRAP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BOOTSTRAP)


@pytest.fixture
def valid_config():
    return {
        "control_url": "https://control.example.test",
        "node_id": str(uuid.uuid4()),
        "enrollment_token": "A" * 48,
        "agent_image": "registry.example.test/ai-agent/node-agent@sha256:" + "a" * 64,
    }


def test_validate_config_accepts_digest_pinned_agent_image(valid_config):
    assert BOOTSTRAP.validate_config(valid_config) == valid_config


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("control_url", "http://control.example.test", "CONTROL_URL_INVALID"),
        ("control_url", "https://user:pass@control.example.test", "CONTROL_URL_INVALID"),
        ("control_url", "https://control.example.test/api", "CONTROL_URL_INVALID"),
        ("control_url", "https://control.example.test?token=x", "CONTROL_URL_INVALID"),
        ("node_id", "not-a-uuid", "NODE_ID_INVALID"),
        ("node_id", 12345, "NODE_ID_INVALID"),
        ("enrollment_token", "short", "ENROLLMENT_TOKEN_INVALID"),
        (
            "agent_image",
            "registry.example.test/node-agent:latest",
            "AGENT_IMAGE_NOT_PINNED_BY_DIGEST",
        ),
    ],
)
def test_validate_config_rejects_unsafe_values(valid_config, field, value, error):
    valid_config[field] = value

    with pytest.raises(BOOTSTRAP.BootstrapError, match=error):
        BOOTSTRAP.validate_config(valid_config)


@pytest.mark.parametrize("value", [None, [], "not-an-object"])
def test_validate_config_rejects_non_object_config(value):
    with pytest.raises(BOOTSTRAP.BootstrapError, match="BOOTSTRAP_CONFIG_INVALID"):
        BOOTSTRAP.validate_config(value)


def test_validate_config_rejects_missing_required_fields(valid_config):
    del valid_config["agent_image"]

    with pytest.raises(BOOTSTRAP.BootstrapError, match="BOOTSTRAP_CONFIG_FIELDS_MISSING"):
        BOOTSTRAP.validate_config(valid_config)


def test_certificate_exchange_response_requires_consumed_token_and_matching_identity():
    node_id = str(uuid.uuid4())
    pem = "-----BEGIN CERTIFICATE-----\nMAA=\n-----END CERTIFICATE-----\n"
    response = {
        "agent_token": "B" * 48,
        "client_certificate": pem,
        "ca_chain": pem,
        "fingerprint_sha256": "c" * 64,
        "node_id": node_id,
        "token_consumed": True,
    }

    result = BOOTSTRAP._validate_certificate_exchange_response(response, node_id)

    assert result == (
        response["agent_token"],
        response["client_certificate"],
        response["ca_chain"],
        response["fingerprint_sha256"],
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("node_id", str(uuid.uuid4())),
        ("token_consumed", False),
        ("fingerprint_sha256", "invalid"),
        ("agent_token", "short"),
        ("client_certificate", ""),
        ("client_certificate", "leaf-certificate"),
        ("ca_chain", ""),
        ("ca_chain", "issuer-certificate"),
    ],
)
def test_certificate_exchange_response_rejects_invalid_fields(field, value):
    node_id = str(uuid.uuid4())
    response = {
        "agent_token": "B" * 48,
        "client_certificate": "-----BEGIN CERTIFICATE-----\nMAA=\n-----END CERTIFICATE-----\n",
        "ca_chain": "-----BEGIN CERTIFICATE-----\nMAA=\n-----END CERTIFICATE-----\n",
        "fingerprint_sha256": "c" * 64,
        "node_id": node_id,
        "token_consumed": True,
    }
    response[field] = value

    with pytest.raises(
        BOOTSTRAP.BootstrapError,
        match="CLIENT_CERTIFICATE_RESPONSE_INVALID",
    ):
        BOOTSTRAP._validate_certificate_exchange_response(response, node_id)



def test_read_secret_files_returns_persisted_values(tmp_path, monkeypatch):
    token = "B" * 48
    payload = {"data": {"token": base64.b64encode(token.encode("ascii")).decode("ascii")}}

    def run(command, check, text, capture_output):
        return type(
            "Completed",
            (),
            {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""},
        )()

    monkeypatch.setattr(BOOTSTRAP.subprocess, "run", run)
    files = BOOTSTRAP._read_secret_files(
        "eph-node-agent-credentials",
        "ai-agent-system",
        ("token",),
        tmp_path,
    )

    assert files["token"].read_text(encoding="ascii") == token
    assert files["token"].stat().st_mode & 0o777 == 0o600


def test_read_secret_files_returns_none_only_for_not_found(tmp_path, monkeypatch):
    def run(command, check, text, capture_output):
        return type(
            "Completed",
            (),
            {"returncode": 1, "stdout": "", "stderr": "Error from server (NotFound)"},
        )()

    monkeypatch.setattr(BOOTSTRAP.subprocess, "run", run)

    assert BOOTSTRAP._read_secret_files(
        "eph-node-agent-credentials",
        "ai-agent-system",
        ("token",),
        tmp_path,
    ) is None



@pytest.mark.parametrize(
    "stderr",
    [
        "connection refused",
        "connection refused: NotFound appears in unrelated diagnostic text",
        "forbidden: user is not allowed to read this resource",
    ],
)
def test_read_secret_files_does_not_treat_unrelated_errors_as_not_found(
    tmp_path,
    monkeypatch,
    stderr,
):
    def run(command, check, text, capture_output):
        return type(
            "Completed",
            (),
            {"returncode": 1, "stdout": "", "stderr": stderr},
        )()

    monkeypatch.setattr(BOOTSTRAP.subprocess, "run", run)

    with pytest.raises(
        BOOTSTRAP.BootstrapError,
        match="KUBERNETES_SECRET_READ_FAILED",
    ):
        BOOTSTRAP._read_secret_files(
            "eph-node-agent-credentials",
            "ai-agent-system",
            ("token",),
            tmp_path,
        )


def test_read_secret_files_rejects_other_kubectl_errors(tmp_path, monkeypatch):
    def run(command, check, text, capture_output):
        return type(
            "Completed",
            (),
            {"returncode": 1, "stdout": "", "stderr": "connection refused"},
        )()

    monkeypatch.setattr(BOOTSTRAP.subprocess, "run", run)

    with pytest.raises(
        BOOTSTRAP.BootstrapError,
        match="KUBERNETES_SECRET_READ_FAILED",
    ):
        BOOTSTRAP._read_secret_files(
            "eph-node-agent-credentials",
            "ai-agent-system",
            ("token",),
            tmp_path,
        )


def test_read_secret_files_rejects_invalid_secret_data(tmp_path, monkeypatch):
    payload = {"data": {"token": "not-base64"}}

    def run(command, check, text, capture_output):
        return type(
            "Completed",
            (),
            {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""},
        )()

    monkeypatch.setattr(BOOTSTRAP.subprocess, "run", run)

    with pytest.raises(
        BOOTSTRAP.BootstrapError,
        match="KUBERNETES_SECRET_DATA_INVALID",
    ):
        BOOTSTRAP._read_secret_files(
            "eph-node-agent-credentials",
            "ai-agent-system",
            ("token",),
            tmp_path,
        )
