from __future__ import annotations

import json

from apps.target_agent.eph_cleanup import (
    EphCleanupError,
    execute_eph_cleanup,
)


def _proof():
    return {
        "eph_pivot_attestation": {
            "source_cluster_uid": "source-uid",
            "target_cluster_uid": "target-uid",
            "source_bmh_digest": "a" * 64,
            "target_bmh_digest": "a" * 64,
            "source_agent_id": "eph-agent-1",
            "command_id": "command-1",
            "attested_at": "2026-11-18T00:00:00Z",
        },
    }


def _resource(kind, name, credential=False):
    annotations = {
        "ai-agent-stack.io/eph-cleanup": "true",
        "ai-agent-stack.io/source-cluster-uid": "source-uid",
    }
    if credential:
        annotations["ai-agent-stack.io/credential-scope"] = "eph-agent"
    return {
        "apiVersion": "v1",
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": "eph-system",
            "uid": f"{kind}-{name}-uid",
            "annotations": annotations,
            "labels": {
                "app.kubernetes.io/managed-by": "ai-agent-stack",
                "ai-agent-stack.io/eph-cleanup": "true",
                "ai-agent-stack.io/deployment-id": "deployment-1",
            },
        },
    }


def _runner_factory():
    resources = {
        "deployments.apps": [_resource("Deployment", "eph-controller")],
        "statefulsets.apps": [],
        "daemonsets.apps": [],
        "jobs.batch": [],
        "pods": [_resource("Pod", "eph-agent")],
        "configmaps": [],
        "secrets": [_resource("Secret", "eph-agent-credentials", credential=True)],
    }
    calls = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[0] == "get" and "--selector" in arguments:
            kind = arguments[1]
            return {
                "available": True,
                "stdout": json.dumps({"items": resources[kind]}),
                "stderr": "",
            }
        if arguments[0] == "get":
            kind = arguments[1]
            name = arguments[2]
            item = next(
                (item for item in resources[kind] if item["metadata"]["name"] == name),
                None,
            )
            if item is None:
                return {"available": False, "stdout": "", "stderr": "Error from server (NotFound)"}
            return {"available": True, "stdout": json.dumps(item), "stderr": ""}
        if arguments[0] == "delete":
            kind = arguments[1]
            name = arguments[2]
            resources[kind] = [
                item for item in resources[kind] if item["metadata"]["name"] != name
            ]
            return {"available": True, "stdout": "deleted", "stderr": ""}
        raise AssertionError(arguments)

    return runner, resources, calls


def test_eph_cleanup_requires_trusted_pivot_attestation(tmp_path):
    kubeconfig = tmp_path / "source.kubeconfig"
    kubeconfig.write_text("source credentials", encoding="utf-8")
    try:
        execute_eph_cleanup(
            {
                "namespace": "eph-system",
                "deployment_id": "deployment-1",
                "confirm_source_cluster_uid": "source-uid",
                "confirm_target_cluster_uid": "target-uid",
            },
            {},
            lambda *_args, **_kwargs: {},
            environ={"CAPI_PIVOT_SOURCE_KUBECONFIG": str(kubeconfig)},
        )
    except EphCleanupError as exc:
        assert str(exc) == "EPH_CLEANUP_TRUSTED_PIVOT_ATTESTATION_REQUIRED"
    else:
        raise AssertionError("Untrusted cleanup must be rejected")


def test_eph_cleanup_deletes_only_attested_resources_and_revokes_credentials(tmp_path):
    kubeconfig = tmp_path / "source.kubeconfig"
    kubeconfig.write_text("source credentials", encoding="utf-8")
    runner, resources, calls = _runner_factory()
    result = execute_eph_cleanup(
        {
            "namespace": "eph-system",
            "deployment_id": "deployment-1",
            "confirm_source_cluster_uid": "source-uid",
            "confirm_target_cluster_uid": "target-uid",
        },
        _proof(),
        runner,
        environ={"CAPI_PIVOT_SOURCE_KUBECONFIG": str(kubeconfig)},
    )
    assert result["eph_cleanup_verified"] is True
    assert result["eph_credentials_revoked"] is True
    assert result["deleted_resource_count"] == 3
    assert result["revoked_credential_count"] == 1
    assert all(not items for items in resources.values())
    assert any(call[0] == "delete" and call[1] == "secrets" for call in calls)


def test_eph_cleanup_rejects_wrong_source_cluster_confirmation(tmp_path):
    kubeconfig = tmp_path / "source.kubeconfig"
    kubeconfig.write_text("source credentials", encoding="utf-8")
    try:
        execute_eph_cleanup(
            {
                "namespace": "eph-system",
                "deployment_id": "deployment-1",
                "confirm_source_cluster_uid": "other-source",
                "confirm_target_cluster_uid": "target-uid",
            },
            _proof(),
            lambda *_args, **_kwargs: {},
            environ={"CAPI_PIVOT_SOURCE_KUBECONFIG": str(kubeconfig)},
        )
    except EphCleanupError as exc:
        assert str(exc) == "EPH_CLEANUP_SOURCE_CLUSTER_CONFIRMATION_MISMATCH"
    else:
        raise AssertionError("Mismatched source cluster must be rejected")
