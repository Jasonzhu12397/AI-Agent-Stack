from __future__ import annotations

import base64
import copy
import json

from apps.eph_agent.cleanup_scope import (
    CleanupScopeError,
    attest_cleanup_scope,
)

DEPLOYMENT_ID = "deployment-1"
SOURCE_UID = "source-cluster-uid"


def _resource(kind, name, namespaced=True, source_uid=None):
    metadata = {
        "name": name,
        "uid": f"uid-{name}",
        "labels": {
            "app.kubernetes.io/managed-by": "ai-agent-stack",
            "ai-agent-stack.io/eph-cleanup": "true",
            "ai-agent-stack.io/deployment-id": DEPLOYMENT_ID,
        },
        "annotations": {"ai-agent-stack.io/eph-cleanup": "true"},
    }
    if namespaced:
        metadata["namespace"] = "ai-agent-system"
    if source_uid:
        metadata["annotations"]["ai-agent-stack.io/source-cluster-uid"] = source_uid
    document = {"kind": kind, "metadata": metadata}
    if kind == "Secret" and name == "capi-pivot-target-kubeconfig":
        document["data"] = {
            "kubeconfig": base64.b64encode(b"target kubeconfig").decode("ascii"),
        }
    if kind == "Deployment":
        document["spec"] = {
            "template": {
                "metadata": {
                    "labels": copy.deepcopy(metadata["labels"]),
                    "annotations": {"ai-agent-stack.io/eph-cleanup": "true"},
                },
            },
        }
    return document


def _target_kubeconfig(tmp_path):
    path = tmp_path / "target.kubeconfig"
    path.write_bytes(b"target kubeconfig")
    return str(path)


def _source_kubeconfig(tmp_path):
    path = tmp_path / "source.kubeconfig"
    path.write_bytes(b"source kubeconfig")
    return str(path)


def _runner_factory(include_optional_target_kubeconfig=True):
    source_objects = {
        "deployments.apps": [_resource("Deployment", "eph-agent")],
        "statefulsets.apps": [],
        "daemonsets.apps": [],
        "jobs.batch": [],
        "pods": [_resource("Pod", "eph-agent-abc")],
        "configmaps": [],
        "secrets": [
            _resource("Secret", "eph-agent-credentials"),
            _resource("Secret", "agent-control-plane-mtls"),
            _resource("Secret", "capi-pivot-source-kubeconfig"),
        ],
        "clusterrolebindings.rbac.authorization.k8s.io": [
            _resource("ClusterRoleBinding", "eph-agent-preflight", namespaced=False)
        ],
        "clusterroles.rbac.authorization.k8s.io": [
            _resource("ClusterRole", "eph-agent-preflight", namespaced=False)
        ],
        "serviceaccounts": [_resource("ServiceAccount", "eph-agent")],
    }
    if include_optional_target_kubeconfig:
        target_kubeconfig_secret = _resource("Secret", "capi-pivot-target-kubeconfig")
        target_kubeconfig_secret["data"] = {
            "kubeconfig": base64.b64encode(b"target kubeconfig").decode("ascii"),
        }
        source_objects["secrets"].append(target_kubeconfig_secret)
    observer_secret = _resource("Secret", "target-agent-source-observer")
    observer_secret["data"] = {
        "kubeconfig": base64.b64encode(b"source kubeconfig").decode("ascii"),
    }
    target_objects = {
        "secrets": [observer_secret],
    }
    calls = []

    def apply_patch(document, operations):
        for operation in operations:
            if operation["op"] == "test":
                assert document["metadata"]["uid"] == operation["value"]
                continue
            path = operation["path"].strip("/").split("/")
            path = [part.replace("~1", "/").replace("~0", "~") for part in path]
            current = document
            for part in path[:-1]:
                current = current.setdefault(part, {})
            current[path[-1]] = operation["value"]

    def runner(command, kubeconfig, **_kwargs):
        calls.append(command)
        arguments = command[3:]
        if arguments[0] == "get":
            resource = {"secret": "secrets"}.get(arguments[1], arguments[1])
            if "--selector" in arguments:
                items = source_objects.get(resource, [])
                return {
                    "ok": True,
                    "stdout": json.dumps({"items": items}),
                    "stderr": "",
                }
            name = arguments[2]
            objects = (
                source_objects.get(resource, [])
                if kubeconfig.endswith("source.kubeconfig")
                else target_objects.get(resource, [])
            )
            item = next(
                (obj for obj in objects if obj["metadata"]["name"] == name),
                None,
            )
            if item is None:
                return {"ok": False, "stdout": "", "stderr": "NotFound"}
            return {"ok": True, "stdout": json.dumps(item), "stderr": ""}
        if arguments[0] == "patch":
            resource = {"secret": "secrets"}.get(arguments[1], arguments[1])
            name = arguments[2]
            objects = (
                source_objects.get(resource, [])
                if kubeconfig == "source.kubeconfig"
                else target_objects.get(resource, [])
            )
            item = next(obj for obj in objects if obj["metadata"]["name"] == name)
            patch = json.loads(arguments[arguments.index("-p") + 1])
            apply_patch(item, patch)
            return {"ok": True, "stdout": "patched", "stderr": ""}
        raise AssertionError(command)

    return runner, source_objects, target_objects, calls


def test_cleanup_scope_attests_source_resources_and_target_observer_secret(tmp_path):
    runner, source, target, calls = _runner_factory()
    result = attest_cleanup_scope(
        _source_kubeconfig(tmp_path),
        _target_kubeconfig(tmp_path),
        DEPLOYMENT_ID,
        SOURCE_UID,
        runner,
    )
    assert result["cleanup_scope_attested"] is True
    assert result["target_observer_secret_source_uid_attested"] is True
    assert result["optional_target_kubeconfig_marked"] is True
    assert result["mutated_resource_count"] > 0
    for items in source.values():
        for item in items:
            assert (
                item["metadata"]["annotations"]["ai-agent-stack.io/source-cluster-uid"]
                == SOURCE_UID
            )
    deployment = source["deployments.apps"][0]
    assert (
        deployment["spec"]["template"]["metadata"]["annotations"][
            "ai-agent-stack.io/source-cluster-uid"
        ]
        == SOURCE_UID
    )
    observer = target["secrets"][0]
    assert (
        observer["metadata"]["annotations"]["ai-agent-stack.io/source-cluster-uid"]
        == SOURCE_UID
    )
    patches = [call for call in calls if call[3] == "patch"]
    assert patches
    assert all("--type=json" in call for call in patches)


def test_cleanup_scope_accepts_absent_optional_target_kubeconfig_secret(tmp_path):
    runner, _source, _target, _calls = _runner_factory(
        include_optional_target_kubeconfig=False
    )
    target_kubeconfig = tmp_path / "target.kubeconfig"
    target_kubeconfig.write_bytes(b"target kubeconfig")
    result = attest_cleanup_scope(
        _source_kubeconfig(tmp_path),
        str(target_kubeconfig),
        DEPLOYMENT_ID,
        SOURCE_UID,
        runner,
    )
    assert result["cleanup_scope_attested"] is True
    assert result["optional_target_kubeconfig_marked"] is False


def test_cleanup_scope_rejects_source_uid_conflict_without_overwriting_it(tmp_path):
    runner, source, _target, _calls = _runner_factory()
    source["secrets"][0]["metadata"]["annotations"][
        "ai-agent-stack.io/source-cluster-uid"
    ] = "different-source-uid"
    try:
        target_kubeconfig = tmp_path / "target.kubeconfig"
        target_kubeconfig.write_bytes(b"target kubeconfig")
        attest_cleanup_scope(
            _source_kubeconfig(tmp_path),
            str(target_kubeconfig),
            DEPLOYMENT_ID,
            SOURCE_UID,
            runner,
        )
    except CleanupScopeError as exc:
        assert str(exc) == "EPH_CLEANUP_SCOPE_SOURCE_UID_MISMATCH"
    else:
        raise AssertionError("Conflicting source UID evidence must not be overwritten")


def test_cleanup_scope_rejects_target_observer_secret_from_another_deployment(tmp_path):
    runner, _source, target, _calls = _runner_factory()
    target["secrets"][0]["metadata"]["labels"][
        "ai-agent-stack.io/deployment-id"
    ] = "other-deployment"
    try:
        target_kubeconfig = tmp_path / "target.kubeconfig"
        target_kubeconfig.write_bytes(b"target kubeconfig")
        attest_cleanup_scope(
            _source_kubeconfig(tmp_path),
            str(target_kubeconfig),
            DEPLOYMENT_ID,
            SOURCE_UID,
            runner,
        )
    except CleanupScopeError as exc:
        assert str(exc) == "EPH_CLEANUP_SCOPE_RESOURCE_OWNERSHIP_MISMATCH"
    else:
        raise AssertionError("Foreign observer secret must not be attested")


def test_cleanup_scope_reports_partial_mutation_when_patch_fails(tmp_path):
    runner, _source, _target, _calls = _runner_factory()
    patch_count = 0

    def fail_after_first_patch(command, kubeconfig, **kwargs):
        nonlocal patch_count
        if command[3] == "patch":
            patch_count += 1
            if patch_count == 2:
                return {"ok": False, "stdout": "", "stderr": "simulated patch timeout"}
        return runner(command, kubeconfig, **kwargs)

    try:
        target_kubeconfig = tmp_path / "target.kubeconfig"
        target_kubeconfig.write_bytes(b"target kubeconfig")
        attest_cleanup_scope(
            _source_kubeconfig(tmp_path),
            str(target_kubeconfig),
            DEPLOYMENT_ID,
            SOURCE_UID,
            fail_after_first_patch,
        )
    except CleanupScopeError as exc:
        assert exc.side_effects is True
    else:
        raise AssertionError("Partial cleanup scope annotation must report side effects")



def test_cleanup_scope_rejects_unrelated_optional_target_kubeconfig_secret(tmp_path):
    runner, source, _target, calls = _runner_factory()
    source["secrets"][-1]["data"]["kubeconfig"] = base64.b64encode(
        b"unrelated kubeconfig"
    ).decode("ascii")

    try:
        attest_cleanup_scope(
            _source_kubeconfig(tmp_path),
            _target_kubeconfig(tmp_path),
            DEPLOYMENT_ID,
            SOURCE_UID,
            runner,
        )
    except CleanupScopeError as exc:
        assert str(exc) == "EPH_CLEANUP_SCOPE_TARGET_KUBECONFIG_SECRET_IDENTITY_MISMATCH"
    else:
        raise AssertionError("An unrelated secret must not be adopted for EPH cleanup")

    assert not any(call[3] == "patch" for call in calls)



def test_cleanup_scope_rejects_observer_secret_with_unrelated_source_kubeconfig(tmp_path):
    runner, _source, target, calls = _runner_factory()
    target["secrets"][0]["data"] = {
        "kubeconfig": base64.b64encode(b"another source kubeconfig").decode("ascii"),
    }
    try:
        attest_cleanup_scope(
            _source_kubeconfig(tmp_path),
            _target_kubeconfig(tmp_path),
            DEPLOYMENT_ID,
            SOURCE_UID,
            runner,
        )
    except CleanupScopeError as exc:
        assert str(exc) == "EPH_CLEANUP_SCOPE_TARGET_OBSERVER_SECRET_IDENTITY_MISMATCH"
    else:
        raise AssertionError("Target observer Secret must contain the attested source kubeconfig")
    assert not any(
        call[3] == "patch" and call[5] == "target-agent-source-observer"
        for call in calls
        if len(call) > 5
    )
