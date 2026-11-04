import subprocess
from pathlib import Path

import pytest

from apps.node_agent.capi_pivot import CapiPivotError, run_capi_pivot_dry_run


def _environment(tmp_path: Path) -> dict[str, str]:
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    source = secret_dir / "source.kubeconfig"
    target = secret_dir / "target.kubeconfig"
    source.write_text("source", encoding="utf-8")
    target.write_text("target", encoding="utf-8")
    return {
        "CAPI_PIVOT_SECRET_DIR": str(secret_dir),
        "CAPI_PIVOT_SOURCE_KUBECONFIG": str(source),
        "CAPI_PIVOT_TARGET_KUBECONFIG": str(target),
        "PATH": "/usr/bin",
        "HOME": "/tmp",
    }


def test_dry_run_uses_fixed_clusterctl_arguments_and_source_environment(tmp_path):
    env = _environment(tmp_path)
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "dry run complete", "")

    result = run_capi_pivot_dry_run(
        {"cluster_name": "target-cluster", "namespace": "metal3"},
        environ=env,
        executable_lookup=lambda name: "/usr/bin/clusterctl",
        process_runner=runner,
    )

    command, kwargs = calls[0]
    assert command == [
        "/usr/bin/clusterctl",
        "move",
        "--dry-run",
        "--to-kubeconfig",
        str(Path(env["CAPI_PIVOT_TARGET_KUBECONFIG"]).resolve()),
        "--namespace",
        "metal3",
        "-v",
        "5",
    ]
    assert kwargs["env"]["KUBECONFIG"] == str(
        Path(env["CAPI_PIVOT_SOURCE_KUBECONFIG"]).resolve()
    )
    assert kwargs["timeout"] == 300
    assert result["dry_run_passed"] is True
    assert result["side_effects"] is False
    assert result["namespace_scope_warning"] is True


def test_dry_run_rejects_kubeconfig_outside_secret_mount(tmp_path):
    env = _environment(tmp_path)
    outside = tmp_path / "outside.kubeconfig"
    outside.write_text("not-secret-mounted", encoding="utf-8")
    env["CAPI_PIVOT_TARGET_KUBECONFIG"] = str(outside)

    with pytest.raises(CapiPivotError, match="CAPI_PIVOT_KUBECONFIG_PATH_INVALID"):
        run_capi_pivot_dry_run(
            {"cluster_name": "target-cluster"},
            environ=env,
            executable_lookup=lambda name: "/usr/bin/clusterctl",
            process_runner=lambda *args, **kwargs: None,
        )


def test_dry_run_rejects_invalid_namespace(tmp_path):
    env = _environment(tmp_path)
    with pytest.raises(CapiPivotError, match="INVALID_NAMESPACE"):
        run_capi_pivot_dry_run(
            {"cluster_name": "target-cluster", "namespace": "../default"},
            environ=env,
            executable_lookup=lambda name: "/usr/bin/clusterctl",
            process_runner=lambda *args, **kwargs: None,
        )


def test_dry_run_requires_clusterctl(tmp_path):
    with pytest.raises(CapiPivotError, match="CLUSTERCTL_NOT_INSTALLED"):
        run_capi_pivot_dry_run(
            {"cluster_name": "target-cluster"},
            environ=_environment(tmp_path),
            executable_lookup=lambda name: None,
            process_runner=lambda *args, **kwargs: None,
        )


def test_dry_run_reports_nonzero_exit_without_exposing_process_output(tmp_path):
    env = _environment(tmp_path)

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            "",
            "authorization: Bearer very-secret-token",
        )

    with pytest.raises(CapiPivotError, match="CAPI_PIVOT_DRY_RUN_FAILED"):
        run_capi_pivot_dry_run(
            {"cluster_name": "target-cluster"},
            environ=env,
            executable_lookup=lambda name: "/usr/bin/clusterctl",
            process_runner=runner,
        )


def test_dry_run_converts_timeout_to_stable_error(tmp_path):
    env = _environment(tmp_path)

    def runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 300)

    with pytest.raises(CapiPivotError, match="CAPI_PIVOT_DRY_RUN_TIMEOUT"):
        run_capi_pivot_dry_run(
            {"cluster_name": "target-cluster"},
            environ=env,
            executable_lookup=lambda name: "/usr/bin/clusterctl",
            process_runner=runner,
        )
