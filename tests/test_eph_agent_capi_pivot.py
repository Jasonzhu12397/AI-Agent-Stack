from threading import Event

import pytest

from apps.eph_agent import capi_pivot


def test_provider_version_parser_rejects_unversioned_image():
    with pytest.raises(capi_pivot.CapiPivotError, match="VERSION_INVALID"):
        capi_pivot._image_version("registry.example.test/capm3:latest", {})


def test_provider_version_parser_uses_deployment_version_annotation():
    deployment = {
        "metadata": {
            "annotations": {"ai-agent-stack.io/provider-version": "v1.10.5"}
        }
    }
    assert capi_pivot._image_version("registry.example.test/capm3@sha256:abc", deployment) == (
        1,
        10,
        5,
    )


def test_bmh_identity_requires_stable_hardware_identifier():
    with pytest.raises(capi_pivot.CapiPivotError, match="HARDWARE_IDENTITY_MISSING"):
        capi_pivot._hardware_identity({
            "spec": {"bootMACAddress": "aa:bb:cc:dd:ee:ff"},
            "status": {"hardware": {}},
        })


def test_capi_pivot_blocks_when_approval_digest_does_not_match_plan(monkeypatch, tmp_path):
    source = tmp_path / "source.kubeconfig"
    target = tmp_path / "target.kubeconfig"
    source.write_text("source", encoding="utf-8")
    target.write_text("target", encoding="utf-8")
    monkeypatch.setenv("CAPI_PIVOT_SOURCE_KUBECONFIG", str(source))
    monkeypatch.setenv("CAPI_PIVOT_TARGET_KUBECONFIG", str(target))
    monkeypatch.setattr(
        capi_pivot,
        "_kubernetes_cluster_uid",
        lambda path: "source-api" if path == str(source) else "target-api",
    )
    monkeypatch.setattr(
        capi_pivot,
        "_provider_preflight",
        lambda *_args: {"target_ready_master_names": []},
    )
    monkeypatch.setattr(
        capi_pivot,
        "_cluster",
        lambda kubeconfig, _namespace, _name: (
            {"metadata": {"uid": "cluster-uid", "annotations": {}}}
            if kubeconfig == str(source)
            else None
        ),
    )
    monkeypatch.setattr(capi_pivot, "_bmh_inventory", lambda *_args: [
        {"namespace": "metal3", "name": "host-1", "hardware_identity": "system-uuid:abc"}
    ])
    monkeypatch.setattr(
        capi_pivot,
        "_kubectl",
        lambda *_args: {"items": [{"metadata": {"name": "cluster-1"}}]},
    )
    result = capi_pivot.execute_capi_pivot({
        "action": "eph.capi.pivot",
        "approval_digest": "a" * 64,
        "payload": {
            "deployment_id": "deployment-1",
            "namespace": "metal3",
            "cluster_name": "cluster-1",
            "stable_cluster_identity": "logical-cluster-1",
            "expected_source_cluster_uid": "cluster-uid",
            "expected_source_bmh_identities": [
                {"namespace": "metal3", "name": "host-1", "hardware_identity": "system-uuid:abc"}
            ],
            "plan_digest": "b" * 64,
        },
    })
    assert result["status"] == "blocked"
    assert result["error_code"] == "CAPI_PIVOT_APPROVAL_PLAN_DIGEST_MISMATCH"
    assert result["side_effects"] is False


def test_capi_pivot_dry_run_never_marks_side_effects(monkeypatch, tmp_path):
    source = tmp_path / "source.kubeconfig"
    target = tmp_path / "target.kubeconfig"
    source.write_text("source", encoding="utf-8")
    target.write_text("target", encoding="utf-8")
    monkeypatch.setenv("CAPI_PIVOT_SOURCE_KUBECONFIG", str(source))
    monkeypatch.setenv("CAPI_PIVOT_TARGET_KUBECONFIG", str(target))
    monkeypatch.setattr(
        capi_pivot,
        "_kubernetes_cluster_uid",
        lambda path: "source-api" if path == str(source) else "target-api",
    )
    monkeypatch.setattr(capi_pivot, "_provider_preflight", lambda *_args: {
        "target_ready_master_names": ["master-1", "master-2", "master-3"],
        "target_controllers": {"ready": True},
        "provider_versions": [],
    })
    monkeypatch.setattr(capi_pivot, "_cluster", lambda _kubeconfig, _namespace, _name: (
        {"metadata": {"uid": "source-cluster-uid", "annotations": {}}}
        if _kubeconfig == str(source)
        else None
    ))
    bmhs = [
        {"namespace": "metal3", "name": "host-1", "hardware_identity": "system-uuid:abc"}
    ]
    monkeypatch.setattr(capi_pivot, "_bmh_inventory", lambda *_args: bmhs)
    monkeypatch.setattr(
        capi_pivot,
        "_kubectl",
        lambda *_args: {"items": [{"metadata": {"name": "cluster-1"}}]},
    )
    calls = []
    monkeypatch.setattr(capi_pivot, "_run", lambda command, *_args, **_kwargs: (
        calls.append(command) or {"ok": True, "stdout": "dry run output", "returncode": 0}
    ))
    result = capi_pivot.execute_capi_pivot({
        "action": "eph.capi.pivot.dry_run",
        "payload": {
            "deployment_id": "deployment-1",
            "namespace": "metal3",
            "cluster_name": "cluster-1",
            "stable_cluster_identity": "logical-cluster-1",
            "expected_source_cluster_uid": "source-cluster-uid",
            "expected_source_bmh_identities": bmhs,
            "provider_deployments": [],
            "target_controller_deployments": [],
        },
    }, Event())
    assert result["status"] == "succeeded"
    assert result["side_effects"] is False
    assert "--dry-run" in calls[0]



def test_provider_version_check_rejects_target_older_than_source(monkeypatch):
    deployments = {
        "source-provider": {
            "status": {"readyReplicas": 1},
            "spec": {"template": {"spec": {"containers": [
                {"name": "manager", "image": "registry.example.test/provider:v1.10.5"}
            ]}}},
        },
        "target-provider": {
            "status": {"readyReplicas": 1},
            "spec": {"template": {"spec": {"containers": [
                {"name": "manager", "image": "registry.example.test/provider:v1.10.4"}
            ]}}},
        },
    }
    monkeypatch.setattr(
        capi_pivot,
        "_deployment",
        lambda _kubeconfig, _namespace, name: deployments[name],
    )
    with pytest.raises(capi_pivot.CapiPivotError, match="OLDER_THAN_SOURCE"):
        capi_pivot._provider_versions(
            "source.kubeconfig",
            "target.kubeconfig",
            [{
                "provider": "capm3",
                "source": {"namespace": "system", "name": "source-provider"},
                "target": {"namespace": "system", "name": "target-provider"},
            }],
        )


def test_provider_version_check_accepts_target_same_as_source(monkeypatch):
    deployment = {
        "status": {"readyReplicas": 1},
        "spec": {"template": {"spec": {"containers": [
            {"name": "manager", "image": "registry.example.test/provider:v1.10.5"}
        ]}}},
    }
    monkeypatch.setattr(
        capi_pivot,
        "_deployment",
        lambda *_args: deployment,
    )
    result = capi_pivot._provider_versions(
        "source.kubeconfig",
        "target.kubeconfig",
        [{
            "provider": "capm3",
            "source": {"namespace": "system", "name": "source-provider"},
            "target": {"namespace": "system", "name": "target-provider"},
        }],
    )
    assert result[0]["target_version_at_least_source"] is True



def test_approved_capi_pivot_verifies_new_target_uid_and_bmh_inventory(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "source.kubeconfig"
    target = tmp_path / "target.kubeconfig"
    source.write_text("source", encoding="utf-8")
    target.write_text("target", encoding="utf-8")
    monkeypatch.setenv("CAPI_PIVOT_SOURCE_KUBECONFIG", str(source))
    monkeypatch.setenv("CAPI_PIVOT_TARGET_KUBECONFIG", str(target))
    monkeypatch.setattr(
        capi_pivot,
        "_kubernetes_cluster_uid",
        lambda path: "source-api" if path == str(source) else "target-api",
    )
    preflight = {
        "target_ready_master_names": ["master-1", "master-2", "master-3"],
        "target_controllers": {"ready": True},
        "provider_versions": [{"provider": "capm3", "target_version_at_least_source": True}],
    }
    monkeypatch.setattr(capi_pivot, "_provider_preflight", lambda *_args: preflight)
    source_cluster = {"metadata": {"uid": "source-cluster-uid", "annotations": {}}}
    target_cluster = {
        "metadata": {
            "uid": "target-cluster-uid",
            "annotations": {
                capi_pivot.SOURCE_UID_ANNOTATION: "source-cluster-uid",
                capi_pivot.STABLE_ID_ANNOTATION: "logical-cluster-1",
            },
        }
    }
    cluster_reads = []
    def read_cluster(kubeconfig, _namespace, _name):
        cluster_reads.append(kubeconfig)
        if len(cluster_reads) == 1:
            return source_cluster
        if len(cluster_reads) == 2:
            return None
        if len(cluster_reads) == 3:
            return target_cluster
        return None
    monkeypatch.setattr(capi_pivot, "_cluster", read_cluster)
    bmhs = [
        {
            "namespace": "metal3",
            "name": "host-1",
            "hardware_identity": "system-uuid:abc",
        }
    ]
    monkeypatch.setattr(
        capi_pivot,
        "_bmh_inventory",
        lambda *_args: bmhs,
    )
    monkeypatch.setattr(
        capi_pivot,
        "_kubectl",
        lambda *_args: {"items": [{"metadata": {"name": "cluster-1"}}]},
    )
    monkeypatch.setattr(
        capi_pivot,
        "_target_controllers_ready",
        lambda *_args: {"ready": True, "controllers": []},
    )
    monkeypatch.setattr(
        capi_pivot,
        "attest_cleanup_scope",
        lambda *_args, **_kwargs: {
            "cleanup_scope_attested": True,
            "source_cleanup_resource_count": 6,
            "target_observer_secret_source_uid_attested": True,
            "optional_target_kubeconfig_marked": True,
            "mutated_resource_count": 0,
        },
    )
    commands = []
    def run(command, *_args, **_kwargs):
        commands.append(command)
        return {"ok": True, "stdout": "completed", "returncode": 0}
    monkeypatch.setattr(capi_pivot, "_run", run)
    result = capi_pivot.execute_capi_pivot({
        "action": "eph.capi.pivot",
        "approval_digest": "a" * 64,
        "payload": {
            "deployment_id": "deployment-1",
            "namespace": "metal3",
            "cluster_name": "cluster-1",
            "stable_cluster_identity": "logical-cluster-1",
            "expected_source_cluster_uid": "source-cluster-uid",
            "expected_source_bmh_identities": bmhs,
            "provider_deployments": [{"provider": "capm3"}],
            "target_controller_deployments": [{"namespace": "system", "name": "capm3"}],
            "plan_digest": "a" * 64,
            "move_timeout_seconds": 60,
        },
    })
    assert result["status"] == "succeeded"
    assert result["side_effects"] is True
    assert result["output"]["source_cluster_uid"] == "source-cluster-uid"
    assert result["output"]["target_cluster_uid"] == "target-cluster-uid"
    assert result["output"]["source_bmh_identities"] == result["output"]["target_bmh_identities"]
    assert commands[0][0] == "kubectl"
    assert commands[1][0] == "clusterctl"


def test_capi_pivot_replay_emits_complete_hardware_attestation(monkeypatch, tmp_path):
    source = tmp_path / "source.kubeconfig"
    target = tmp_path / "target.kubeconfig"
    source.write_text("source", encoding="utf-8")
    target.write_text("target", encoding="utf-8")
    monkeypatch.setenv("CAPI_PIVOT_SOURCE_KUBECONFIG", str(source))
    monkeypatch.setenv("CAPI_PIVOT_TARGET_KUBECONFIG", str(target))
    monkeypatch.setattr(
        capi_pivot,
        "_kubernetes_cluster_uid",
        lambda path: "source-api" if path == str(source) else "target-api",
    )
    monkeypatch.setattr(
        capi_pivot,
        "_provider_preflight",
        lambda *_args: {
            "target_ready_master_names": ["master-1", "master-2", "master-3"],
            "target_controllers": {"ready": True},
        },
    )
    identities = [{
        "namespace": "metal3",
        "name": "host-1",
        "hardware_identity": "system-uuid:abc",
    }]
    target_cluster = {
        "metadata": {
            "uid": "target-cluster-uid",
            "annotations": {
                capi_pivot.SOURCE_UID_ANNOTATION: "source-cluster-uid",
                capi_pivot.STABLE_ID_ANNOTATION: "logical-cluster-1",
            },
        }
    }
    monkeypatch.setattr(
        capi_pivot,
        "_cluster",
        lambda kubeconfig, *_args: (
            target_cluster if kubeconfig == str(target) else None
        ),
    )
    monkeypatch.setattr(capi_pivot, "_bmh_inventory", lambda *_args: identities)
    monkeypatch.setattr(
        capi_pivot,
        "attest_cleanup_scope",
        lambda *_args, **_kwargs: {
            "cleanup_scope_attested": True,
            "source_cleanup_resource_count": 6,
            "target_observer_secret_source_uid_attested": True,
            "optional_target_kubeconfig_marked": True,
            "mutated_resource_count": 0,
        },
    )
    result = capi_pivot.execute_capi_pivot({
        "action": "eph.capi.pivot",
        "approval_digest": "a" * 64,
        "payload": {
            "deployment_id": "deployment-1",
            "namespace": "metal3",
            "cluster_name": "cluster-1",
            "stable_cluster_identity": "logical-cluster-1",
            "expected_source_cluster_uid": "source-cluster-uid",
            "expected_source_bmh_identities": identities,
        },
    })
    assert result["status"] == "succeeded"
    assert result["side_effects"] is False
    assert result["output"]["already_pivoted"] is True
    assert result["output"]["capi_pivot_succeeded"] is True
    assert result["output"]["target_cluster_ownership_verified"] is True
    assert result["output"]["source_bmh_digest"] == result["output"]["target_bmh_digest"]


def test_failed_capi_move_reports_possible_side_effects(monkeypatch, tmp_path):
    source = tmp_path / "source.kubeconfig"
    target = tmp_path / "target.kubeconfig"
    source.write_text("source", encoding="utf-8")
    target.write_text("target", encoding="utf-8")
    monkeypatch.setenv("CAPI_PIVOT_SOURCE_KUBECONFIG", str(source))
    monkeypatch.setenv("CAPI_PIVOT_TARGET_KUBECONFIG", str(target))
    monkeypatch.setattr(
        capi_pivot,
        "_kubernetes_cluster_uid",
        lambda path: "source-api" if path == str(source) else "target-api",
    )
    monkeypatch.setattr(capi_pivot, "_provider_preflight", lambda *_args: {
        "target_ready_master_names": ["master-1", "master-2", "master-3"],
        "target_controllers": {"ready": True},
        "provider_versions": [],
    })
    source_cluster = {"metadata": {"uid": "source-cluster-uid", "annotations": {}}}
    monkeypatch.setattr(
        capi_pivot,
        "_cluster",
        lambda kubeconfig, *_args: source_cluster if kubeconfig == str(source) else None,
    )
    bmhs = [{
        "namespace": "metal3",
        "name": "host-1",
        "hardware_identity": "system-uuid:abc",
    }]
    monkeypatch.setattr(capi_pivot, "_bmh_inventory", lambda *_args: bmhs)
    monkeypatch.setattr(capi_pivot, "_kubectl", lambda *_args: {
        "items": [{"metadata": {"name": "cluster-1"}}]
    })
    calls = []
    def run(_command, *_args, **_kwargs):
        calls.append(True)
        if len(calls) == 1:
            return {"ok": True, "stdout": "annotated", "returncode": 0}
        return {"ok": False, "stdout": "", "stderr": "move failed", "returncode": 1}
    monkeypatch.setattr(capi_pivot, "_run", run)
    result = capi_pivot.execute_capi_pivot({
        "action": "eph.capi.pivot",
        "approval_digest": "a" * 64,
        "payload": {
            "deployment_id": "deployment-1",
            "namespace": "metal3",
            "cluster_name": "cluster-1",
            "stable_cluster_identity": "logical-cluster-1",
            "expected_source_cluster_uid": "source-cluster-uid",
            "expected_source_bmh_identities": bmhs,
            "provider_deployments": [{"provider": "capm3"}],
            "target_controller_deployments": [{"namespace": "system", "name": "capm3"}],
            "plan_digest": "a" * 64,
            "move_timeout_seconds": 60,
        },
    })
    assert result["status"] == "blocked"
    assert result["side_effects"] is True



def test_target_provider_controllers_must_be_ready_on_control_plane_masters(monkeypatch):
    deployment = {
        "metadata": {"generation": 2},
        "spec": {
            "selector": {"matchLabels": {"app": "capm3"}},
            "template": {
                "spec": {
                    "tolerations": [{
                        "key": "node-role.kubernetes.io/control-plane",
                        "operator": "Exists",
                        "effect": "NoSchedule",
                    }]
                }
            },
        },
        "status": {
            "observedGeneration": 2,
            "readyReplicas": 1,
            "availableReplicas": 1,
        },
    }
    pods = {
        "items": [{
            "metadata": {"name": "capm3-controller"},
            "spec": {"nodeName": "master-1"},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }]
    }
    monkeypatch.setattr(capi_pivot, "_deployment", lambda *_args: deployment)
    monkeypatch.setattr(capi_pivot, "_kubectl", lambda *_args: pods)
    result = capi_pivot._target_controllers_ready(
        "target.kubeconfig",
        {"master-1", "master-2", "master-3"},
        [{"namespace": "metal3-system", "name": "capm3-controller", "minimum_ready_replicas": 1}],
    )
    assert result["ready"] is True
    assert result["controllers"][0]["scheduled_on_target_masters"] is True


def test_target_provider_controllers_without_master_toleration_are_blocked(monkeypatch):
    deployment = {
        "metadata": {"generation": 1},
        "spec": {
            "selector": {"matchLabels": {"app": "capm3"}},
            "template": {"spec": {"tolerations": []}},
        },
        "status": {"observedGeneration": 1, "readyReplicas": 1, "availableReplicas": 1},
    }
    monkeypatch.setattr(capi_pivot, "_deployment", lambda *_args: deployment)
    with pytest.raises(capi_pivot.CapiPivotError, match="MISSING_MASTER_TOLERATION"):
        capi_pivot._target_controllers_ready(
            "target.kubeconfig",
            {"master-1", "master-2", "master-3"},
            [{"namespace": "metal3-system", "name": "capm3-controller"}],
        )
