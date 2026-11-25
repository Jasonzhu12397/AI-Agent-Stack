from apps.target_agent.write_permissions import (
    NETWORK_APPLY_PERMISSIONS,
    WORKER_PROVISION_PERMISSIONS,
    WritePermissionError,
    verify_write_permissions,
)


def _runner(answer: str = "yes", calls: list[list[str]] | None = None):
    def run(arguments, **kwargs):
        if calls is not None:
            calls.append(arguments)
        return {
            "available": True,
            "returncode": 0,
            "stdout": f"{answer}\n",
            "stderr": "",
        }

    return run


def test_worker_provision_checks_every_required_permission_in_target_namespace():
    calls: list[list[str]] = []
    verify_write_permissions(
        "target.worker.provision",
        runner=_runner(calls=calls),
        namespace="capi-system",
    )

    assert len(calls) == len(WORKER_PROVISION_PERMISSIONS)
    for arguments, permission in zip(calls, WORKER_PROVISION_PERMISSIONS, strict=True):
        assert arguments[:4] == [
            "auth",
            "can-i",
            permission.verb,
            permission.resource,
        ]
        if permission.namespace_scoped:
            assert arguments[-2:] == ["--namespace", "capi-system"]
        else:
            assert "--namespace" not in arguments


def test_denied_permission_blocks_before_the_first_write():
    calls: list[list[str]] = []
    try:
        verify_write_permissions(
            "target.network.apply",
            runner=_runner("no", calls),
            namespace="capi-system",
        )
    except WritePermissionError as exc:
        assert str(exc).startswith("TARGET_RBAC_INSUFFICIENT:")
    else:  # pragma: no cover - fail closed assertion
        raise AssertionError("missing permission was accepted")
    assert len(calls) == 1


def test_permission_probe_failure_is_not_treated_as_an_allow():
    def unavailable(arguments, **kwargs):
        return {"available": False, "error": "kubectl unavailable"}

    try:
        verify_write_permissions(
            "target.network.apply",
            runner=unavailable,
            namespace="capi-system",
        )
    except WritePermissionError as exc:
        assert str(exc) == "TARGET_WRITE_PERMISSION_CHECK_UNAVAILABLE"
    else:  # pragma: no cover
        raise AssertionError("unavailable kubectl was accepted")


def test_unknown_answer_is_rejected():
    try:
        verify_write_permissions(
            "target.worker.provision",
            runner=_runner("maybe"),
            namespace="capi-system",
        )
    except WritePermissionError as exc:
        assert str(exc) == "TARGET_WRITE_PERMISSION_ANSWER_INVALID"
    else:  # pragma: no cover
        raise AssertionError("unparseable answer was accepted")


def test_network_apply_requires_cluster_scoped_crd_and_storageclass_writes():
    required = {(item.verb, item.resource) for item in NETWORK_APPLY_PERMISSIONS}

    assert ("get", "customresourcedefinitions.apiextensions.k8s.io") in required
    assert ("create", "storageclasses.storage.k8s.io") in required
