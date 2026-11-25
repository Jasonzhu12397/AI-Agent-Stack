import hashlib
from pathlib import Path

import pytest
import yaml

from apps.eph_agent.target_bootstrap import (
    TargetBootstrapError,
    execute_target_agent_bootstrap,
)

CREDENTIAL_KEYS = (
    "control-url",
    "agent-id",
    "installation-id",
    "cluster-id",
    "ironic-api-url",
    "ironic-api-token",
    "ironic-internal-ntp-ip",
    "approved-external-ntp-sources",
    "worker-network-cidrs",
    "required-controller-deployments",
)


def _bundle_documents() -> list[dict]:
    credentials = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "target-agent-credentials", "namespace": "ai-agent-system"},
        "stringData": {key: "value" for key in CREDENTIAL_KEYS},
    }
    mtls = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "agent-control-plane-mtls", "namespace": "ai-agent-system"},
        "stringData": {"ca.crt": "ca", "tls.crt": "crt", "tls.key": "key"},
    }
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "target-agent", "namespace": "ai-agent-system"},
        "spec": {
            "replicas": 3,
            "selector": {"matchLabels": {"app": "target-agent"}},
            "template": {
                "metadata": {"labels": {"app": "target-agent"}},
                "spec": {
                    "securityContext": {"runAsNonRoot": True},
                    "containers": [
                        {
                            "name": "target-agent",
                            "image": "registry.example.test/target-agent@sha256:abc",
                            "securityContext": {},
                            "volumeMounts": [],
                        }
                    ],
                    "volumes": [
                        {"name": "tmp", "emptyDir": {}},
                    ],
                },
            },
        },
    }
    return [credentials, mtls, deployment]


def _write_bundle(tmp_path: Path, documents=None) -> tuple[Path, Path]:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    path = bundle_dir / "bundle.yaml"
    path.write_text(
        yaml.safe_dump_all(documents or _bundle_documents(), sort_keys=False),
        encoding="utf-8",
    )
    kubeconfig = tmp_path / "target.kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    return bundle_dir, kubeconfig


def _runner(calls: list[list[str]], ready: int = 3, rollout: int = 0):
    def run(arguments, **kwargs):
        calls.append(arguments)
        joined = " ".join(arguments)
        if arguments[:2] == ["apply", "--server-side"]:
            return {"available": True, "returncode": 0, "stdout": "applied", "stderr": ""}
        if joined.startswith("rollout status"):
            return {"available": True, "returncode": rollout, "stdout": "", "stderr": ""}
        if joined.startswith("get namespace"):
            return {
                "available": True,
                "returncode": 0,
                "stdout": '{"metadata":{"uid":"target-uid"}}',
                "stderr": "",
            }
        if joined.startswith("get deployment"):
            return {
                "available": True,
                "returncode": 0,
                "stdout": f'{{"status":{{"readyReplicas":{ready}}}}}',
                "stderr": "",
            }
        raise AssertionError(f"unexpected kubectl call: {joined}")

    return run


def test_bootstrap_applies_the_sealed_bundle_and_reports_live_identity(tmp_path):
    bundle_dir, kubeconfig = _write_bundle(tmp_path)
    calls: list[list[str]] = []
    evidence = execute_target_agent_bootstrap(
        {},
        runner=_runner(calls),
        environ={
            "TARGET_AGENT_BOOTSTRAP_DIR": str(bundle_dir),
            "CAPI_PIVOT_TARGET_KUBECONFIG": str(kubeconfig),
        },
    )

    assert evidence["target_agent_bootstrapped"] is True
    assert evidence["target_cluster_uid"] == "target-uid"
    assert evidence["ready_replicas"] == 3
    assert evidence["side_effects"] is True
    assert evidence["document_count"] == 3
    assert calls[0][:2] == ["apply", "--server-side"]


def test_bootstrap_rejects_a_bundle_digest_mismatch_before_applying(tmp_path):
    bundle_dir, kubeconfig = _write_bundle(tmp_path)
    calls: list[list[str]] = []
    with pytest.raises(TargetBootstrapError, match="DIGEST_MISMATCH"):
        execute_target_agent_bootstrap(
            {"bundle_sha256": "0" * 64},
            runner=_runner(calls),
            environ={
                "TARGET_AGENT_BOOTSTRAP_DIR": str(bundle_dir),
                "CAPI_PIVOT_TARGET_KUBECONFIG": str(kubeconfig),
            },
        )
    assert calls == []


def test_bootstrap_rejects_host_path_volumes(tmp_path):
    documents = _bundle_documents()
    deployment = documents[-1]
    deployment["spec"]["template"]["spec"]["volumes"] = [
        {"name": "host", "hostPath": {"path": "/"}}
    ]
    bundle_dir, kubeconfig = _write_bundle(tmp_path, documents)
    with pytest.raises(TargetBootstrapError, match="VOLUME_FORBIDDEN:hostPath"):
        execute_target_agent_bootstrap(
            {},
            runner=_runner([]),
            environ={
                "TARGET_AGENT_BOOTSTRAP_DIR": str(bundle_dir),
                "CAPI_PIVOT_TARGET_KUBECONFIG": str(kubeconfig),
            },
        )


def test_bootstrap_requires_the_sealed_bundle(tmp_path):
    kubeconfig = tmp_path / "target.kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    with pytest.raises(TargetBootstrapError, match="BUNDLE_REQUIRED"):
        execute_target_agent_bootstrap(
            {},
            runner=_runner([]),
            environ={
                "TARGET_AGENT_BOOTSTRAP_DIR": str(tmp_path / "missing"),
                "CAPI_PIVOT_TARGET_KUBECONFIG": str(kubeconfig),
            },
        )


def test_bootstrap_requires_the_target_kubeconfig(tmp_path):
    with pytest.raises(TargetBootstrapError, match="TARGET_KUBECONFIG_REQUIRED"):
        execute_target_agent_bootstrap(
            {},
            runner=_runner([]),
            environ={
                "TARGET_AGENT_BOOTSTRAP_DIR": str(tmp_path / "missing"),
                "CAPI_PIVOT_TARGET_KUBECONFIG": str(tmp_path / "missing-kubeconfig"),
            },
        )


def test_bootstrap_rollout_failure_is_a_side_effect(tmp_path):
    bundle_dir, kubeconfig = _write_bundle(tmp_path)
    with pytest.raises(TargetBootstrapError) as exc_info:
        execute_target_agent_bootstrap(
            {},
            runner=_runner([], rollout=1),
            environ={
                "TARGET_AGENT_BOOTSTRAP_DIR": str(bundle_dir),
                "CAPI_PIVOT_TARGET_KUBECONFIG": str(kubeconfig),
            },
        )
    assert exc_info.value.code == "TARGET_AGENT_BOOTSTRAP_ROLLOUT_FAILED"
    assert exc_info.value.side_effects is True


def test_bundle_digest_is_the_stable_file_digest(tmp_path):
    bundle_dir, kubeconfig = _write_bundle(tmp_path)
    expected = hashlib.sha256((bundle_dir / "bundle.yaml").read_bytes()).hexdigest()
    evidence = execute_target_agent_bootstrap(
        {"bundle_sha256": expected},
        runner=_runner([]),
        environ={
            "TARGET_AGENT_BOOTSTRAP_DIR": str(bundle_dir),
            "CAPI_PIVOT_TARGET_KUBECONFIG": str(kubeconfig),
        },
    )
    assert evidence["bundle_sha256"] == expected


def test_eph_action_requires_the_target_control_plane_ready_checkpoint():
    from apps.eph_agent.main import execute

    result = execute(
        {
            "action": "eph.target.agent.bootstrap",
            "deployment_checkpoint": {"phase": "target_control_plane_applied"},
            "payload": {},
        }
    )
    assert result["status"] == "blocked"
    assert result["error_code"] == "TARGET_CONTROL_PLANE_READY_CHECKPOINT_REQUIRED"
    assert result["side_effects"] is False
