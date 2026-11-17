import pytest
from node_agent.chrony import (
    ChronyError,
    _render_config,
    _validate_sources,
    configure_chrony,
)


def test_worker_must_use_exact_ironic_private_ip_only():
    assert _validate_sources("target_worker", ["10.130.60.84"], "10.130.60.84") == [
        "10.130.60.84"
    ]


@pytest.mark.parametrize(
    "sources,endpoint",
    [
        (["pool.ntp.org"], "10.130.60.84"),
        (["192.0.2.10"], "10.130.60.84"),
        (["10.130.60.84", "pool.ntp.org"], "10.130.60.84"),
        (["10.130.60.85"], "10.130.60.84"),
    ],
)
def test_worker_rejects_external_or_wrong_ntp_sources(sources, endpoint):
    with pytest.raises(ChronyError):
        _validate_sources("target_worker", sources, endpoint)


def test_control_plane_uses_external_sources_and_serves_internal_clients():
    config = _render_config(
        "target_control_plane",
        ["time.example.net", "192.0.2.20"],
        ["10.130.60.80/28"],
        ironic_internal_ip="10.130.60.84",
    )
    assert "server time.example.net iburst" in config
    assert "allow 10.130.60.80/28" in config
    assert "bindaddress 10.130.60.84" in config
    assert "port 123" in config
    assert "cmdport 0" in config


def test_control_plane_rejects_public_client_network():
    with pytest.raises(ChronyError, match="CONTROL_PLANE_NTP_ALLOWED_CLIENT_MUST_BE_PRIVATE"):
        _render_config(
            "target_control_plane",
            ["time.example.net"],
            ["0.0.0.0/0"],
            ironic_internal_ip="10.130.60.84",
        )


def test_eph_uses_bootstrap_sources_without_ntp_server_mode():
    config = _render_config("eph", ["10.20.0.10"], [])
    assert "server 10.20.0.10 iburst" in config
    assert "allow " not in config
    assert "port 123" not in config


def test_worker_config_never_contains_external_sources():
    config = _render_config("target_worker", ["10.130.60.84"], [])
    assert "server 10.130.60.84 iburst" in config
    assert "pool " not in config
    assert "pool.ntp.org" not in config


def test_configure_requires_root(monkeypatch, tmp_path):
    monkeypatch.setattr("node_agent.chrony.os.geteuid", lambda: 10001)
    with pytest.raises(ChronyError, match="CHRONY_ROOT_REQUIRED"):
        configure_chrony(
            "eph", ["10.20.0.10"], root=str(tmp_path), require_root=True
        )


def test_configure_rejects_unsupported_os(tmp_path, monkeypatch):
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/os-release").write_text('ID="alpine"\n', encoding="utf-8")
    monkeypatch.setattr("node_agent.chrony.os.geteuid", lambda: 0)
    with pytest.raises(ChronyError, match="CHRONY_OS_UNSUPPORTED"):
        configure_chrony(
            "eph",
            ["10.20.0.10"],
            root=str(tmp_path),
            require_root=False,
            executable_lookup=lambda name: "/usr/bin/" + name,
        )


def test_configure_rejects_offline_root_instead_of_running_host_mutations(tmp_path, monkeypatch):
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/os-release").write_text('ID="ubuntu"\n', encoding="utf-8")
    monkeypatch.setattr("node_agent.chrony.os.geteuid", lambda: 0)
    with pytest.raises(ChronyError, match="CHRONY_OFFLINE_ROOT_EXECUTION_UNSUPPORTED"):
        configure_chrony(
            "eph",
            ["10.20.0.10"],
            root=str(tmp_path),
            require_root=False,
            executable_lookup=lambda name: "/usr/bin/" + name,
        )


def test_configure_does_not_mutate_an_offline_root(tmp_path, monkeypatch):
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/os-release").write_text('ID="ubuntu"\n', encoding="utf-8")
    monkeypatch.setattr("node_agent.chrony.os.geteuid", lambda: 0)
    with pytest.raises(ChronyError, match="CHRONY_OFFLINE_ROOT_EXECUTION_UNSUPPORTED"):
        configure_chrony(
            "eph",
            ["10.20.0.10"],
            root=str(tmp_path),
            require_root=False,
            executable_lookup=lambda name: "/usr/bin/" + name,
        )



def test_managed_config_removes_unapproved_fallback_sources(tmp_path):
    from node_agent.chrony import _write_managed_config

    path = tmp_path / "chrony.conf"
    path.write_text(
        "pool 0.pool.ntp.org iburst\n"
        "server 192.0.2.99 iburst\n"
        "driftfile /var/lib/chrony/drift\n"
        "sourcedir /run/chrony-dhcp\n",
        encoding="utf-8",
    )
    block = (
        "# BEGIN AI-AGENT-STACK MANAGED CHRONY\n"
        "server 10.130.60.84 iburst\n"
        "# END AI-AGENT-STACK MANAGED CHRONY\n"
    )
    _write_managed_config(path, block)
    result = path.read_text(encoding="utf-8")
    assert "0.pool.ntp.org" not in result
    assert "192.0.2.99" not in result
    assert "/run/chrony-dhcp" not in result
    assert "server 10.130.60.84 iburst" in result
    assert "driftfile /var/lib/chrony/drift" in result


def test_control_plane_requires_private_ironic_internal_bind_address():
    with pytest.raises(ChronyError, match="CONTROL_PLANE_IRONIC_INTERNAL_IP_REQUIRED"):
        _render_config(
            "target_control_plane",
            ["time.example.net"],
            ["10.130.60.80/28"],
        )
