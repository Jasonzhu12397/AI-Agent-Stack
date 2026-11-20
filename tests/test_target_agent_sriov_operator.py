import json
from pathlib import Path

import pytest

from apps.target_agent.sriov_operator import (
    SriovOperatorError,
    execute_sriov_operator,
)

OPERATOR_DOCS = [
    {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {"name": "sriovnetworknodepolicies.sriovnetwork.openshift.io"},
    },
    {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "sriov-network-operator", "namespace": "sriov-network-operator"},
    },
]
POLICY = {
    "apiVersion": "sriovnetwork.openshift.io/v1",
    "kind": "SriovNetworkNodePolicy",
    "metadata": {"name": "numa0"},
    "spec": {"resourceName": "intelNuma0", "numSrs": 4},
}


class Runner:
    def __init__(self, ready_after: int = 1, ready: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.ready_after = ready_after
        self.ready = ready
        self.polls = 0
        self.applied: list[str] = []

    def __call__(self, args, timeout=0, max_output_bytes=0):
        self.calls.append(args)
        if args[:2] == ["get", "deployment"]:
            self.polls += 1
            replicas = self.ready_after if self.ready else 0
            document = {"spec": {"replicas": 1}, "status": {"readyReplicas": replicas}}
            return {"available": True, "returncode": 0, "stdout": json.dumps(document)}
        if args[:2] == ["apply", "--server-side"]:
            content = Path(args[-1]).read_text(encoding="utf-8")
            assert "kind:" in content
            self.applied.append(content)
        return {"available": True, "returncode": 0, "stdout": ""}


def _run(runner, policies, tmp_path, **kwargs):
    manifest = tmp_path / "operator.yaml"
    import yaml

    manifest.write_text(yaml.safe_dump_all(OPERATOR_DOCS), encoding="utf-8")
    return execute_sriov_operator(
        policies,
        runner=runner,
        manifest=manifest.read_text(encoding="utf-8"),
        sleep=lambda _: None,
        monotonic=lambda: 0.0,
        timeout_seconds=60,
        **kwargs,
    )


def test_no_policy_is_a_noop():
    runner = Runner()
    result = execute_sriov_operator([], runner=runner)
    assert result["sriov_operator_installed"] is False
    assert result["sriov_policies_expected"] is False
    assert runner.calls == []


def test_installs_crds_then_operator_then_policy(tmp_path):
    runner = Runner()
    result = _run(runner, [POLICY], tmp_path)
    kinds = [
        [line for line in content.splitlines() if line.startswith("kind:")]
        for content in runner.applied
    ]
    assert kinds[0] == ["kind: CustomResourceDefinition"]
    assert "kind: Deployment" in kinds[1]
    assert "kind: SriovNetworkNodePolicy" in kinds[2]
    wait = next(call for call in runner.calls if call[0] == "wait")
    assert "crd/sriovnetworknodepolicies.sriovnetwork.openshift.io" in wait
    assert result["sriov_operator_ready"] is True
    assert result["sriov_policies_applied"] == ["numa0"]
    assert result["side_effects"] is True


def test_operator_ready_timeout_is_reported(tmp_path):
    runner = Runner(ready=False)
    with pytest.raises(SriovOperatorError) as excinfo:
        execute_sriov_operator(
            [POLICY],
            runner=runner,
            manifest=__import__("yaml").safe_dump_all(OPERATOR_DOCS),
            sleep=lambda _: None,
            monotonic=lambda: 0.0,
            timeout_seconds=60,
        )
    assert excinfo.value.code == "SRIOV_OPERATOR_READY_TIMEOUT"


def test_lease_loss_stops_before_apply(tmp_path):
    import threading

    event = threading.Event()
    event.set()
    runner = Runner()
    with pytest.raises(SriovOperatorError) as excinfo:
        _run(runner, [POLICY], tmp_path, lease_lost_event=event)
    assert excinfo.value.code == "SRIOV_OPERATOR_LEASE_LOST"


def test_invalid_policy_shape_is_rejected():
    with pytest.raises(SriovOperatorError) as excinfo:
        execute_sriov_operator([{"kind": "Deployment"}], runner=Runner())
    assert excinfo.value.code == "SRIOV_POLICIES_INVALID"
