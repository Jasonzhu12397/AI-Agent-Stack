from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from tools import metal3_bmc_secrets


@dataclass
class _Machine:
    spec: SimpleNamespace
    username: str
    password: str


def machine(name: str, username: str = "bmc-user", password: str = "bmc-pass"):
    return _Machine(
        spec=SimpleNamespace(secret_ref=f"{name}-bmc"),
        username=username,
        password=password,
    )


def test_renders_namespaced_owner_manifest(tmp_path):
    documents = metal3_bmc_secrets.render_bmc_secrets(
        [machine("node-1"), machine("node-2")],
        namespace="metal3",
    )
    assert [item["metadata"]["name"] for item in documents] == [
        "node-1-bmc",
        "node-2-bmc",
    ]
    assert all(item["metadata"]["namespace"] == "metal3" for item in documents)
    assert documents[0]["stringData"] == {
        "username": "bmc-user",
        "password": "bmc-pass",
    }

    path = tmp_path / "secrets.yaml"
    metal3_bmc_secrets.write_private_manifest(path, documents)
    assert path.stat().st_mode & 0o777 == 0o600
    assert "node-1-bmc" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "username,password,code",
    [
        ("", "bmc-pass", "BMC_SECRET_USERNAME_MISSING"),
        ("bmc-user", "", "BMC_SECRET_PASSWORD_MISSING"),
    ],
)
def test_rejects_empty_credentials(username, password, code):
    with pytest.raises(metal3_bmc_secrets.BmcSecretError, match=code):
        metal3_bmc_secrets.render_bmc_secret(
            machine("node-1", username=username, password=password),
        )


def test_rejects_duplicate_secret_references():
    with pytest.raises(
        metal3_bmc_secrets.BmcSecretError,
        match="BMC_SECRET_REFERENCE_DUPLICATE",
    ):
        metal3_bmc_secrets.render_bmc_secrets(
            [machine("node-1"), machine("node-1")],
        )


def test_rejects_invalid_namespace_and_secret_name():
    with pytest.raises(metal3_bmc_secrets.BmcSecretError, match="NAMESPACE_INVALID"):
        metal3_bmc_secrets.render_bmc_secret(machine("node-1"), namespace="Metal3")
    bad = machine("node-1")
    bad.spec.secret_ref = "Bad_Name"
    with pytest.raises(metal3_bmc_secrets.BmcSecretError, match="REFERENCE_INVALID"):
        metal3_bmc_secrets.render_bmc_secret(bad)
