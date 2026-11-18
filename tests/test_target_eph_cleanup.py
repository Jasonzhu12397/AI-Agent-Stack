from __future__ import annotations

import json

from apps.target_agent import main as target_main
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
            "cleanup_scope_attested": True,
            "target_observer_secret_source_uid_attested": True,
            "source_cleanup_resource_count": 7,
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
    cluster_role = _resource("ClusterRole", "eph-agent-preflight")
    cluster_role_binding = _resource("ClusterRoleBinding", "eph-agent-preflight")
    cluster_role["metadata"].pop("namespace")
    cluster_role_binding["metadata"].pop("namespace")
    resources = {
        "deployments.apps": [_resource("Deployment", "eph-controller")],
        "statefulsets.apps": [],
        "daemonsets.apps": [],
        "jobs.batch": [],
        "pods": [_resource("Pod", "eph-agent")],
        "configmaps": [],
        "secrets": [_resource("Secret", "eph-agent-credentials", credential=True)],
        "clusterrolebindings.rbac.authorization.k8s.io": [cluster_role_binding],
        "clusterroles.rbac.authorization.k8s.io": [cluster_role],
        "serviceaccounts": [_resource("ServiceAccount", "eph-agent")],
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
            kind = {"secret": "secrets"}.get(arguments[1], arguments[1])
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
        if arguments[0] in ("label", "annotate"):
            kind = {"secret": "secrets"}.get(arguments[1], arguments[1])
            name = arguments[2]
            item = next(
                item for item in resources[kind] if item["metadata"]["name"] == name
            )
            target = (
                item["metadata"].setdefault("labels", {})
                if arguments[0] == "label"
                else item["metadata"].setdefault("annotations", {})
            )
            for value in arguments[3:]:
                if "=" in value and not value.startswith("--"):
                    key, label_value = value.split("=", 1)
                    target[key] = label_value
            return {"available": True, "stdout": "updated", "stderr": ""}
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
    assert result["deleted_resource_count"] == 6
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



def test_target_agent_revokes_only_the_attested_source_observer_secret(monkeypatch):
    secret = {
        "metadata": {
            "name": "target-agent-source-observer",
            "namespace": "ai-agent-system",
            "uid": "observer-secret-uid",
            "labels": {
                "app.kubernetes.io/managed-by": "ai-agent-stack",
                "ai-agent-stack.io/eph-cleanup": "true",
                "ai-agent-stack.io/deployment-id": "deployment-1",
            },
            "annotations": {
                "ai-agent-stack.io/eph-cleanup": "true",
                "ai-agent-stack.io/source-cluster-uid": "source-uid",
            },
        },
    }
    responses = iter([
        {"available": True, "stdout": json.dumps(secret), "stderr": ""},
        {"available": True, "stdout": "secret deleted", "stderr": ""},
        {"available": False, "stdout": "", "stderr": "Error from server (NotFound)"},
    ])
    calls = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        return next(responses)

    monkeypatch.setattr(target_main, "_kubectl", runner)
    assert target_main._revoke_source_observer_secret("deployment-1", "source-uid") is True
    assert calls[1][:3] == ["delete", "secret", "target-agent-source-observer"]
    assert "--preconditions=uid=observer-secret-uid" in calls[1]


def test_target_agent_refuses_to_revoke_another_deployments_observer_secret(monkeypatch):
    secret = {
        "metadata": {
            "name": "target-agent-source-observer",
            "namespace": "ai-agent-system",
            "uid": "observer-secret-uid",
            "labels": {
                "app.kubernetes.io/managed-by": "ai-agent-stack",
                "ai-agent-stack.io/eph-cleanup": "true",
                "ai-agent-stack.io/deployment-id": "other-deployment",
            },
            "annotations": {
                "ai-agent-stack.io/eph-cleanup": "true",
                "ai-agent-stack.io/source-cluster-uid": "source-uid",
            },
        },
    }
    monkeypatch.setattr(
        target_main,
        "_kubectl",
        lambda *_args, **_kwargs: {
            "available": True,
            "stdout": json.dumps(secret),
            "stderr": "",
        },
    )
    try:
        target_main._revoke_source_observer_secret("deployment-1", "source-uid")
    except target_main.HandoffError as exc:
        assert str(exc) == "EPH_SOURCE_OBSERVER_SECRET_OWNERSHIP_MISMATCH"
    else:
        raise AssertionError("A secret owned by another deployment must not be revoked")



def test_eph_cleanup_runs_only_after_live_target_ownership_and_uses_separate_namespaces(
    monkeypatch,
):
    context = {"eph_pivot_attestation": {
        "source_cluster_uid": "source-uid",
        "target_cluster_uid": "target-uid",
    }}
    calls = {}

    def observe_ownership(payload, _context):
        calls["ownership_payload"] = payload
        return {
            "target_ironic_inventory_verified": True,
            "target_bmh_identities": [],
        }

    def cleanup_executor(payload, _context, _runner, _lease):
        calls["cleanup_payload"] = payload
        return {
            "eph_credentials_revoked": True,
            "revoked_credential_count": 2,
            "side_effects": True,
        }

    monkeypatch.setattr(
        target_main,
        "_observe_target_metal3_ownership",
        observe_ownership,
    )
    monkeypatch.setattr(target_main, "execute_eph_cleanup", cleanup_executor)
    monkeypatch.setattr(
        target_main,
        "_revoke_source_observer_secret",
        lambda deployment_id, source_uid, _lease: (
            deployment_id == "deployment-1" and source_uid == "source-uid"
        ),
    )
    result = target_main._execute_attested_eph_cleanup(
        {
            "target_namespace": "metal3-system",
            "target_cluster_name": "target-cluster",
            "deployment_id": "deployment-1",
        },
        context,
    )
    assert calls["ownership_payload"]["namespace"] == "metal3-system"
    assert calls["cleanup_payload"]["namespace"] == "ai-agent-system"
    assert calls["cleanup_payload"]["confirm_source_cluster_uid"] == "source-uid"
    assert result["eph_credentials_revoked"] is True
    assert result["target_ironic_inventory_verified"] is True



def test_eph_cleanup_action_requires_completed_bmh_handoff_checkpoint(monkeypatch):
    monkeypatch.setattr(
        target_main,
        "_execute_attested_eph_cleanup",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cleanup must not run before BMH handoff")
        ),
    )
    outcome = target_main.execute({
        "action": "target.eph.cleanup",
        "payload": {},
        "deployment_checkpoint": {"phase": "capi_pivot_verified"},
    })
    assert outcome["status"] == "blocked"
    assert outcome["error_code"] == "EPH_CLEANUP_BMH_HANDOFF_CHECKPOINT_REQUIRED"


def test_eph_cleanup_handoff_phase_does_not_delete_before_bmh_checkpoint(monkeypatch):
    monkeypatch.setattr(
        target_main,
        "_execute_attested_eph_cleanup",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cleanup must not run before BMH handoff")
        ),
    )
    outcome = target_main.execute({
        "action": "target.handoff.evaluate",
        "payload": {
            "next_phase": "eph_cleanup_verified",
            "evidence": {},
        },
        "deployment_checkpoint": {
            "phase": "capi_pivot_verified",
            "sequence": 4,
            "state": {},
        },
    })
    assert outcome["status"] == "blocked"
    assert outcome["error_code"] == "EPH_CLEANUP_BMH_HANDOFF_CHECKPOINT_REQUIRED"



def test_eph_cleanup_is_idempotent_after_resources_were_removed(tmp_path):
    kubeconfig = tmp_path / "source.kubeconfig"
    kubeconfig.write_text("source credentials", encoding="utf-8")
    runner, _resources, _calls = _runner_factory()
    payload = {
        "namespace": "eph-system",
        "deployment_id": "deployment-1",
        "confirm_source_cluster_uid": "source-uid",
        "confirm_target_cluster_uid": "target-uid",
    }
    first = execute_eph_cleanup(
        payload,
        _proof(),
        runner,
        environ={"CAPI_PIVOT_SOURCE_KUBECONFIG": str(kubeconfig)},
    )
    second = execute_eph_cleanup(
        payload,
        _proof(),
        runner,
        environ={"CAPI_PIVOT_SOURCE_KUBECONFIG": str(kubeconfig)},
    )
    assert first["deleted_resource_count"] == 6
    assert second["deleted_resource_count"] == 0
    assert second["eph_workloads_absent"] is True
    assert second["eph_credentials_revoked"] is True



def test_eph_cleanup_discovers_and_revokes_late_target_kubeconfig_secret(tmp_path):
    kubeconfig = tmp_path / "source.kubeconfig"
    kubeconfig.write_text("source credentials", encoding="utf-8")
    runner, resources, calls = _runner_factory()
    target_secret = _resource("Secret", "capi-pivot-target-kubeconfig", credential=True)
    resources["secrets"].append(target_secret)
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
    assert result["revoked_credential_count"] == 2
    assert not resources["secrets"]
    assert any(
        call[0] == "delete"
        and call[1] == "secrets"
        and call[2] == "capi-pivot-target-kubeconfig"
        for call in calls
    )



def test_eph_cleanup_reports_partial_deletion_and_recovers_on_retry(tmp_path):
    kubeconfig = tmp_path / "source.kubeconfig"
    kubeconfig.write_text("source credentials", encoding="utf-8")
    runner, resources, _calls = _runner_factory()
    delete_count = 0

    def fail_second_delete(arguments, **kwargs):
        nonlocal delete_count
        if arguments[0] == "delete":
            delete_count += 1
            if delete_count == 2:
                return {
                    "available": False,
                    "stdout": "",
                    "stderr": "simulated source API timeout",
                }
        return runner(arguments, **kwargs)

    payload = {
        "namespace": "eph-system",
        "deployment_id": "deployment-1",
        "confirm_source_cluster_uid": "source-uid",
        "confirm_target_cluster_uid": "target-uid",
    }
    try:
        execute_eph_cleanup(
            payload,
            _proof(),
            fail_second_delete,
            environ={"CAPI_PIVOT_SOURCE_KUBECONFIG": str(kubeconfig)},
        )
    except EphCleanupError as exc:
        assert exc.side_effects is True
    else:
        raise AssertionError("A partial source cleanup must report side effects")

    result = execute_eph_cleanup(
        payload,
        _proof(),
        runner,
        environ={"CAPI_PIVOT_SOURCE_KUBECONFIG": str(kubeconfig)},
    )
    assert result["eph_cleanup_verified"] is True
    assert all(not items for items in resources.values())



def test_eph_cleanup_refuses_to_adopt_unowned_pivot_kubeconfig_secret(tmp_path):
    kubeconfig = tmp_path / "source.kubeconfig"
    kubeconfig.write_text("source credentials", encoding="utf-8")
    runner, resources, calls = _runner_factory()
    secret = _resource("Secret", "capi-pivot-target-kubeconfig", credential=True)
    secret["metadata"]["labels"] = {}
    secret["metadata"]["annotations"] = {}
    resources["secrets"].append(secret)

    try:
        execute_eph_cleanup(
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
    except EphCleanupError as exc:
        assert str(exc) == "EPH_CLEANUP_TARGET_KUBECONFIG_SECRET_OWNERSHIP_MISMATCH"
    else:
        raise AssertionError("An unowned kubeconfig secret must never be adopted or deleted")

    assert not any(call[0] in ("label", "annotate", "delete") for call in calls)


def test_eph_cleanup_requires_source_uid_attestation_on_every_resource(tmp_path):
    kubeconfig = tmp_path / "source.kubeconfig"
    kubeconfig.write_text("source credentials", encoding="utf-8")
    runner, resources, calls = _runner_factory()
    resources["deployments.apps"][0]["metadata"]["annotations"][
        "ai-agent-stack.io/source-cluster-uid"
    ] = "different-source"

    try:
        execute_eph_cleanup(
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
    except EphCleanupError as exc:
        assert str(exc) == "EPH_CLEANUP_RESOURCE_OWNERSHIP_MISMATCH"
    else:
        raise AssertionError("Resources from a different source cluster must be rejected")

    assert not any(call[0] == "delete" for call in calls)


def test_eph_cleanup_delete_uses_uid_precondition(tmp_path):
    kubeconfig = tmp_path / "source.kubeconfig"
    kubeconfig.write_text("source credentials", encoding="utf-8")
    runner, _resources, calls = _runner_factory()
    execute_eph_cleanup(
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
    delete_calls = [call for call in calls if call[0] == "delete"]
    assert delete_calls
    assert all(
        any(argument.startswith("--preconditions=uid=") for argument in call)
        for call in delete_calls
    )



def test_target_agent_requires_source_uid_on_observer_secret(monkeypatch):
    secret = {
        "metadata": {
            "name": "target-agent-source-observer",
            "namespace": "ai-agent-system",
            "uid": "observer-secret-uid",
            "labels": {
                "app.kubernetes.io/managed-by": "ai-agent-stack",
                "ai-agent-stack.io/eph-cleanup": "true",
                "ai-agent-stack.io/deployment-id": "deployment-1",
            },
            "annotations": {
                "ai-agent-stack.io/eph-cleanup": "true",
            },
        },
    }
    monkeypatch.setattr(
        target_main,
        "_kubectl",
        lambda *_args, **_kwargs: {
            "available": True,
            "stdout": json.dumps(secret),
            "stderr": "",
        },
    )
    try:
        target_main._revoke_source_observer_secret("deployment-1", "source-uid")
    except target_main.HandoffError as exc:
        assert str(exc) == "EPH_SOURCE_OBSERVER_SECRET_OWNERSHIP_MISMATCH"
    else:
        raise AssertionError("Missing source cluster UID must block credential revocation")



def test_target_agent_treats_missing_observer_secret_as_idempotent_noop(monkeypatch):
    calls = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        return {
            "available": False,
            "stdout": "",
            "stderr": "Error from server (NotFound)",
        }

    monkeypatch.setattr(target_main, "_kubectl", runner)
    assert target_main._revoke_source_observer_secret("deployment-1", "source-uid") is False
    assert len(calls) == 1
    assert not any(call[0] == "delete" for call in calls)



def test_target_cleanup_requires_trusted_cleanup_scope_attestation(monkeypatch):
    monkeypatch.setattr(
        target_main,
        "_observe_target_metal3_ownership",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ownership observation must not run without trusted cleanup proof")
        ),
    )
    outcome = target_main.execute({
        "action": "target.eph.cleanup",
        "payload": {
            "deployment_id": "deployment-1",
            "target_namespace": "metal3",
            "target_cluster_name": "target",
            "eph_namespace": "ai-agent-system",
        },
        "deployment_context": {
            "eph_pivot_attestation": {
                "source_cluster_uid": "source-uid",
                "target_cluster_uid": "target-uid",
            },
        },
        "deployment_checkpoint": {"phase": "bmh_registry_migrated"},
    })
    assert outcome["status"] == "blocked"
    assert outcome["error_code"] == "EPH_CLEANUP_SCOPE_ATTESTATION_REQUIRED"
