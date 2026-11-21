"""The upgrade readiness probe reports presence, never secrecy."""

from packages.agent_contracts.kubernetes_upgrade import (
    READINESS_PACKAGE_MANAGER_ENV,
    READINESS_PACKAGE_VERSION_ENV,
    READINESS_REMOTE_USER_ENV,
    readiness_report,
)


def _which(present=("kubeadm", "kubectl")):
    return lambda name: "/usr/local/bin/" + name if name in present else None


def test_a_fully_configured_host_is_execute_ready():
    report = readiness_report(
        {
            READINESS_PACKAGE_MANAGER_ENV: "apt",
            READINESS_PACKAGE_VERSION_ENV: "1.34.1-1.1",
            READINESS_REMOTE_USER_ENV: "metal",
        },
        _which(),
    )

    assert report["execute_ready"] is True
    assert report["package_upgrade_configured"] is True
    assert report["remote_executor_configured"] is True
    assert report["missing"] == []
    assert report["binaries"] == {"kubeadm": True, "kubectl": True}


def test_an_empty_host_names_what_is_missing():
    report = readiness_report({}, _which())

    assert report["execute_ready"] is False
    assert report["missing"] == [
        READINESS_PACKAGE_MANAGER_ENV,
        READINESS_PACKAGE_VERSION_ENV,
        READINESS_REMOTE_USER_ENV,
    ]
    assert report["package_manager"] is None


def test_a_missing_binary_blocks_execution():
    report = readiness_report(
        {
            READINESS_PACKAGE_MANAGER_ENV: "dnf",
            READINESS_PACKAGE_VERSION_ENV: "1.34.1-1500",
            READINESS_REMOTE_USER_ENV: "metal",
        },
        _which(present=("kubectl",)),
    )

    assert report["execute_ready"] is False
    assert report["binaries"]["kubeadm"] is False
    assert any("kubeadm" in line for line in report["detail"])


def test_an_unknown_package_manager_is_reported_not_hidden():
    report = readiness_report(
        {READINESS_PACKAGE_MANAGER_ENV: "yum", READINESS_PACKAGE_VERSION_ENV: "1.34.1"},
        _which(),
    )

    assert report["execute_ready"] is False
    assert report["package_manager"] is None
    assert any("yum" in line for line in report["detail"])


def test_the_probe_never_echoes_a_configured_value():
    secret_like = "metal@example"
    report = readiness_report(
        {READINESS_REMOTE_USER_ENV: secret_like},
        _which(),
    )

    assert secret_like not in str(report)
    assert report["remote_executor_configured"] is True
