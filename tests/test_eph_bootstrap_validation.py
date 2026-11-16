# ruff: noqa: I001
import base64
import importlib.util
import json
import os
import pathlib
import subprocess
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


def test_persisted_client_certificate_accepts_pem_chain_whitespace():
    certificate = (
        "-----BEGIN CERTIFICATE-----\n"
        "MAA=\n"
        "-----END CERTIFICATE-----\n"
    )
    bundle = certificate + "\n" + certificate

    assert BOOTSTRAP._is_pem_certificate_bundle(bundle)
    assert len(
        BOOTSTRAP.re.findall(
            r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
            bundle,
            BOOTSTRAP.re.DOTALL,
        )
    ) == 2


@pytest.mark.parametrize(
    "certificate",
    [
        "",
        "not-a-certificate",
        "-----BEGIN CERTIFICATE-----\nMAA=\n-----END CERTIFICATE-----\ntrailing-data",
    ],
)
def test_persisted_client_certificate_rejects_invalid_pem_bundle(certificate):
    assert not BOOTSTRAP._is_pem_certificate_bundle(certificate)


def test_validate_persisted_client_credentials_accepts_newline_separated_chain(
    tmp_path, monkeypatch
):
    node_id = str(uuid.uuid4())
    certificate = (
        "-----BEGIN CERTIFICATE-----\n"
        "MAA=\n"
        "-----END CERTIFICATE-----\n"
    )
    certificate_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    certificate_path.write_text(certificate + "\n" + certificate, encoding="ascii")
    key_path.write_text("private-key", encoding="ascii")
    outputs = iter(
        [
            "subject=CN=agent-node-" + node_id + ".agents.internal",
            "PUBLIC KEY",
            "PUBLIC KEY",
        ]
    )
    monkeypatch.setattr(BOOTSTRAP, "_run", lambda arguments, input_text=None: None)
    monkeypatch.setattr(
        BOOTSTRAP, "_run_output", lambda arguments, timeout=None: next(outputs)
    )

    BOOTSTRAP._validate_persisted_client_credentials(
        {"tls.crt": certificate_path, "tls.key": key_path},
        tmp_path / "issuer-ca.crt",
        tmp_path,
        node_id,
    )



def test_bootstrap_failure_message_reports_failed_command_without_file_values():
    error = subprocess.CalledProcessError(
        1,
        [
            "kubectl",
            "--kubeconfig",
            "/etc/kubernetes/admin.conf",
            "create",
            "secret",
            "--from-file=token=/tmp/private-token",
        ],
    )
    message = BOOTSTRAP._bootstrap_failure_message(error)
    assert message == (
        "EPH_BOOTSTRAP_COMMAND_FAILED:kubectl:--kubeconfig:"
        "/etc/kubernetes/admin.conf:create:secret:exit=1"
    )
    assert "private-token" not in message


def test_bootstrap_failure_message_reports_os_error_class_and_errno():
    error = OSError(13, "permission denied")
    assert BOOTSTRAP._bootstrap_failure_message(error) == (
        "EPH_BOOTSTRAP_OS_ERROR:PermissionError:errno=13"
    )


def test_bootstrap_failure_message_preserves_safe_bootstrap_error_code():
    error = BOOTSTRAP.BootstrapError("KUBERNETES_API_NOT_READY")
    assert BOOTSTRAP._bootstrap_failure_message(error) == "KUBERNETES_API_NOT_READY"


def test_load_or_create_enrollment_csr_reuses_key_and_request(tmp_path):
    common_name = f"agent-node-{uuid.uuid4()}.agents.internal"
    key_path, csr_path, csr_pem = BOOTSTRAP._load_or_create_enrollment_csr(
        common_name,
        tmp_path,
    )
    first_key = key_path.read_bytes()
    second_key, second_csr, second_pem = BOOTSTRAP._load_or_create_enrollment_csr(
        common_name,
        tmp_path,
    )

    assert second_key == key_path
    assert second_csr == csr_path
    assert second_pem == csr_pem
    assert key_path.read_bytes() == first_key
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert csr_path.stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_load_or_create_enrollment_csr_fails_closed_on_partial_state(tmp_path):
    (tmp_path / "tls.key").write_text("private", encoding="ascii")

    with pytest.raises(
        BOOTSTRAP.BootstrapError, match="LOCAL_ENROLLMENT_STATE_INCOMPLETE"
    ):
        BOOTSTRAP._load_or_create_enrollment_csr(
            f"agent-node-{uuid.uuid4()}.agents.internal",
            tmp_path,
        )


def test_load_or_create_enrollment_csr_fails_closed_on_key_mismatch(tmp_path):
    common_name = f"agent-node-{uuid.uuid4()}.agents.internal"
    key_path, csr_path, _ = BOOTSTRAP._load_or_create_enrollment_csr(
        common_name,
        tmp_path,
    )
    other_key = tmp_path / "other.key"
    subprocess.run(
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "EC",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-out",
            str(other_key),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    os.chmod(str(other_key), 0o600)
    other_key.replace(key_path)

    with pytest.raises(
        BOOTSTRAP.BootstrapError, match="LOCAL_ENROLLMENT_KEY_MISMATCH"
    ):
        BOOTSTRAP._load_or_create_enrollment_csr(common_name, tmp_path),


def test_local_enrollment_state_round_trip_and_secure_permissions(tmp_path):
    common_name = f"agent-node-{uuid.uuid4()}.agents.internal"
    key_path, csr_path, _ = BOOTSTRAP._load_or_create_enrollment_csr(
        common_name,
        tmp_path,
    )
    ca_path = tmp_path / "ca.crt"
    cert_path = tmp_path / "tls.crt"
    ca_path.write_text(
        "-----BEGIN CERTIFICATE-----\\nMAA=\\n-----END CERTIFICATE-----\\n",
        encoding="ascii",
    )
    cert_path.write_text(
        "-----BEGIN CERTIFICATE-----\\nMAA=\\n-----END CERTIFICATE-----\\n",
        encoding="ascii",
    )
    token = "B" * 48
    state = BOOTSTRAP._persist_local_enrollment_state(
        token,
        {"ca.crt": ca_path, "tls.crt": cert_path, "tls.key": key_path},
        tmp_path,
    )

    assert state["token"].read_text(encoding="ascii") == token
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in state.values())


def test_local_enrollment_state_rejects_partial_response_files(tmp_path):
    (tmp_path / "agent-token").write_text("B" * 48, encoding="ascii")
    os.chmod(str(tmp_path / "agent-token"), 0o600)

    with pytest.raises(
        BOOTSTRAP.BootstrapError,
        match="LOCAL_ENROLLMENT_CREDENTIAL_STATE_INCOMPLETE",
    ):
        BOOTSTRAP._load_local_enrollment_state(
            str(uuid.uuid4()),
            tmp_path / "issuer-ca.crt",
            tmp_path,
        )


def test_existing_partial_secret_must_match_local_enrollment_state(tmp_path):
    local_state = {}
    for key, content in (
        ("token", "B" * 48),
        ("ca.crt", "CA CERTIFICATE"),
        ("tls.crt", "CLIENT CERTIFICATE"),
        ("tls.key", "PRIVATE KEY"),
    ):
        path = tmp_path / ("expected-" + key)
        path.write_text(content, encoding="ascii")
        local_state[key] = path
    actual = tmp_path / "actual-token"
    actual.write_text("C" * 48, encoding="ascii")

    with pytest.raises(
        BOOTSTRAP.BootstrapError,
        match="EPH_AGENT_CREDENTIAL_SECRET_STATE_MISMATCH",
    ):
        BOOTSTRAP._assert_existing_secrets_match_local_state(
            {"token": actual},
            None,
            local_state,
        )
