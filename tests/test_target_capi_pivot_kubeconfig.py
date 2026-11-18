import pytest
import yaml

from apps.target_agent import capi_pivot


def test_target_kubeconfig_uses_projected_in_cluster_credentials(tmp_path, monkeypatch):
    service_account_dir = tmp_path / "service-account"
    service_account_dir.mkdir()
    token_path = service_account_dir / "token"
    ca_path = service_account_dir / "ca.crt"
    token_path.write_text("projected-service-account-token", encoding="utf-8")
    ca_path.write_text("cluster-ca", encoding="utf-8")
    kubeconfig_path = tmp_path / "target.kubeconfig"

    monkeypatch.delenv("CAPI_PIVOT_TARGET_KUBECONFIG", raising=False)
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    monkeypatch.setattr(capi_pivot, "SERVICE_ACCOUNT_DIR", service_account_dir)
    monkeypatch.setattr(capi_pivot, "TARGET_KUBECONFIG_PATH", kubeconfig_path)

    result = capi_pivot._ensure_target_kubeconfig()

    assert result == str(kubeconfig_path)
    document = yaml.safe_load(kubeconfig_path.read_text(encoding="utf-8"))
    assert document["clusters"][0]["cluster"]["server"] == "https://10.96.0.1:443"
    assert document["clusters"][0]["cluster"]["certificate-authority"] == str(ca_path)
    assert document["users"][0]["user"]["tokenFile"] == str(token_path)
    assert kubeconfig_path.stat().st_mode & 0o777 == 0o600


def test_explicit_missing_target_kubeconfig_fails_closed(tmp_path, monkeypatch):
    missing_path = tmp_path / "missing-target.kubeconfig"
    monkeypatch.setenv("CAPI_PIVOT_TARGET_KUBECONFIG", str(missing_path))
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")

    with pytest.raises(
        capi_pivot.CapiPivotError,
        match="CAPI_PIVOT_TARGET_KUBECONFIG_MISSING",
    ):
        capi_pivot._ensure_target_kubeconfig()

    assert not missing_path.exists()


def test_target_kubeconfig_brackets_ipv6_service_host(tmp_path, monkeypatch):
    service_account_dir = tmp_path / "service-account"
    service_account_dir.mkdir()
    (service_account_dir / "token").write_text("token", encoding="utf-8")
    (service_account_dir / "ca.crt").write_text("ca", encoding="utf-8")
    kubeconfig_path = tmp_path / "target.kubeconfig"

    monkeypatch.delenv("CAPI_PIVOT_TARGET_KUBECONFIG", raising=False)
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "fd00::1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "6443")
    monkeypatch.setattr(capi_pivot, "SERVICE_ACCOUNT_DIR", service_account_dir)
    monkeypatch.setattr(capi_pivot, "TARGET_KUBECONFIG_PATH", kubeconfig_path)

    capi_pivot._ensure_target_kubeconfig()

    document = yaml.safe_load(kubeconfig_path.read_text(encoding="utf-8"))
    assert document["clusters"][0]["cluster"]["server"] == "https://[fd00::1]:6443"


def test_invalid_target_service_port_is_rejected(tmp_path, monkeypatch):
    service_account_dir = tmp_path / "service-account"
    service_account_dir.mkdir()
    (service_account_dir / "token").write_text("token", encoding="utf-8")
    (service_account_dir / "ca.crt").write_text("ca", encoding="utf-8")

    monkeypatch.delenv("CAPI_PIVOT_TARGET_KUBECONFIG", raising=False)
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "99999")
    monkeypatch.setattr(capi_pivot, "SERVICE_ACCOUNT_DIR", service_account_dir)
    monkeypatch.setattr(capi_pivot, "TARGET_KUBECONFIG_PATH", tmp_path / "target.kubeconfig")

    with pytest.raises(
        capi_pivot.CapiPivotError,
        match="CAPI_PIVOT_TARGET_IN_CLUSTER_CREDENTIALS_UNAVAILABLE",
    ):
        capi_pivot._ensure_target_kubeconfig()
