import json
import subprocess
from pathlib import Path

import pytest
from node_agent.capi_pivot import (
    CapiPivotError,
    run_capi_pivot,
    run_capi_pivot_dry_run,
    run_capi_pivot_postflight,
)


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


def _cluster_list(*names: str) -> str:
    return json.dumps(
        {
            "apiVersion": "cluster.x-k8s.io/v1beta1",
            "kind": "ClusterList",
            "items": [
                {"metadata": {"name": name, "namespace": "metal3"}}
                for name in names
            ],
        }
    )


def _runner_for(cluster_output: str = None, move_returncode: int = 0):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command[0].endswith("kubectl"):
            return subprocess.CompletedProcess(
                command, 0, cluster_output or _cluster_list("target-cluster"), ""
            )
        return subprocess.CompletedProcess(
            command, move_returncode, "dry run complete", ""
        )

    return calls, runner


def _pivot_runner_for(
    target_controller_ready=True,
    pivot_returncode=0,
    bmh_items=None,
    source_ironic_replicas=0,
    target_keepalived=True,
):
    calls = []
    groups = [
        "cluster.x-k8s.io",
        "bootstrap.cluster.x-k8s.io",
        "controlplane.cluster.x-k8s.io",
        "infrastructure.cluster.x-k8s.io",
        "metal3.io",
    ]
    resources = {
        "cluster.x-k8s.io": [
            "clusters", "machines", "machinesets", "machinedeployments"
        ],
        "bootstrap.cluster.x-k8s.io": [
            "kubeadmconfigs", "kubeadmconfigtemplates"
        ],
        "controlplane.cluster.x-k8s.io": ["kubeadmcontrolplanes"],
        "infrastructure.cluster.x-k8s.io": [
            "metal3clusters", "metal3machines", "metal3machinetemplates"
        ],
        "metal3.io": ["baremetalhosts"],
    }
    controllers = [
        "capi-controller-manager",
        "capi-kubeadm-bootstrap-controller-manager",
        "capi-kubeadm-control-plane-controller-manager",
        "capm3-controller-manager",
    ]

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command[0].endswith("clusterctl"):
            if "--dry-run" in command:
                return subprocess.CompletedProcess(command, 0, "dry run complete", "")
            if kwargs["timeout"] == 1800:
                return subprocess.CompletedProcess(
                    command, pivot_returncode, "pivot complete", ""
                )
        if command[0].endswith("kubectl"):
            if "--kubeconfig" in command:
                kubeconfig = command[command.index("--kubeconfig") + 1]
            else:
                kubeconfig = ""
            if kubeconfig.endswith("source.kubeconfig"):
                if "baremetalhosts.metal3.io" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps({"items": bmh_items or []}),
                        "",
                    )
                if "metal3-ironic" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps({
                            "spec": {"replicas": source_ironic_replicas},
                            "status": {"availableReplicas": source_ironic_replicas},
                        }),
                        "",
                    )
                return subprocess.CompletedProcess(
                    command, 0, _cluster_list("target-cluster"), ""
                )
            if "metal3-ironic" in command:
                containers = [{"name": "ironic"}]
                if target_keepalived:
                    containers.append({"name": "ironic-endpoint-keepalived"})
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({
                        "metadata": {"generation": 1},
                        "spec": {
                            "replicas": 1,
                            "template": {"spec": {"containers": containers}},
                        },
                        "status": {"availableReplicas": 1, "observedGeneration": 1},
                    }),
                    "",
                )
            if "get" in command and "--raw" in command:
                raw_path = command[command.index("--raw") + 1]
                if raw_path == "/apis":
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps({
                            "groups": [
                                {
                                    "name": name,
                                    "preferredVersion": {"version": "v1beta1"},
                                }
                                for name in groups
                            ]
                        }),
                        "",
                    )
                group = raw_path[len("/apis/"):].rsplit("/", 1)[0]
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({
                        "resources": [{"name": name} for name in resources[group]]
                    }),
                    "",
                )
            if "deployment" in command:
                name = command[command.index("deployment") + 1]
                ready = target_controller_ready or name != controllers[0]
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({
                        "metadata": {"generation": 1},
                        "spec": {"replicas": 1},
                        "status": {
                            "availableReplicas": 1 if ready else 0,
                            "observedGeneration": 1,
                        },
                    }),
                    "",
                )
            return subprocess.CompletedProcess(command, 0, '{"items":[]}', "")
        raise AssertionError(command)

    return calls, runner


def _execute(tmp_path, runner=None, lookup=None, payload=None):
    env = _environment(tmp_path)
    return run_capi_pivot_dry_run(
        payload or {"cluster_name": "target-cluster", "namespace": "metal3"},
        environ=env,
        executable_lookup=lookup or (lambda name: f"/usr/bin/{name}"),
        process_runner=runner,
    )


def test_dry_run_checks_source_scope_before_clusterctl(tmp_path):
    calls, runner = _runner_for()
    result = _execute(tmp_path, runner=runner)

    assert len(calls) == 2
    kubectl_command, kubectl_kwargs = calls[0]
    assert kubectl_command == [
        "/usr/bin/kubectl",
        "--kubeconfig",
        str((tmp_path / "secrets" / "source.kubeconfig").resolve()),
        "--namespace",
        "metal3",
        "get",
        "clusters.cluster.x-k8s.io",
        "-o",
        "json",
    ]
    assert kubectl_kwargs["timeout"] == 30

    move_command, move_kwargs = calls[1]
    assert move_command == [
        "/usr/bin/clusterctl",
        "move",
        "--dry-run",
        "--to-kubeconfig",
        str((tmp_path / "secrets" / "target.kubeconfig").resolve()),
        "--namespace",
        "metal3",
        "-v",
        "5",
    ]
    assert move_kwargs["env"]["KUBECONFIG"] == str(
        (tmp_path / "secrets" / "source.kubeconfig").resolve()
    )
    assert move_kwargs["timeout"] == 60
    assert result["dry_run_passed"] is True
    assert result["side_effects"] is False
    assert result["source_cluster_count"] == 1
    assert result["namespace_scope_warning"] is True
    assert result["status_subresources_migrated"] is False


def test_dry_run_rejects_namespace_with_multiple_clusters(tmp_path):
    calls, runner = _runner_for(_cluster_list("target-cluster", "other-cluster"))

    with pytest.raises(
        CapiPivotError,
        match="CAPI_PIVOT_NAMESPACE_MUST_CONTAIN_ONLY_TARGET_CLUSTER",
    ):
        _execute(tmp_path, runner=runner)
    assert len(calls) == 1


def test_dry_run_rejects_namespace_without_target_cluster(tmp_path):
    calls, runner = _runner_for(_cluster_list("different-cluster"))

    with pytest.raises(
        CapiPivotError,
        match="CAPI_PIVOT_NAMESPACE_MUST_CONTAIN_ONLY_TARGET_CLUSTER",
    ):
        _execute(tmp_path, runner=runner)
    assert len(calls) == 1


def test_dry_run_rejects_invalid_source_cluster_list(tmp_path):
    calls, runner = _runner_for('{"items": "not-a-list"}')

    with pytest.raises(CapiPivotError, match="CAPI_PIVOT_SOURCE_SCOPE_INVALID"):
        _execute(tmp_path, runner=runner)
    assert len(calls) == 1


def test_dry_run_rejects_source_scope_command_failure(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "forbidden")

    with pytest.raises(CapiPivotError, match="CAPI_PIVOT_SOURCE_SCOPE_FAILED"):
        _execute(tmp_path, runner=runner)
    assert len(calls) == 1


def test_dry_run_rejects_kubeconfig_outside_secret_mount(tmp_path):
    env = _environment(tmp_path)
    outside = tmp_path / "outside.kubeconfig"
    outside.write_text("not-secret-mounted", encoding="utf-8")
    env["CAPI_PIVOT_TARGET_KUBECONFIG"] = str(outside)

    with pytest.raises(CapiPivotError, match="CAPI_PIVOT_KUBECONFIG_PATH_INVALID"):
        run_capi_pivot_dry_run(
            {"cluster_name": "target-cluster"},
            environ=env,
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=lambda *args, **kwargs: None,
        )


def test_dry_run_rejects_invalid_namespace(tmp_path):
    with pytest.raises(CapiPivotError, match="INVALID_NAMESPACE"):
        _execute(
            tmp_path,
            payload={"cluster_name": "target-cluster", "namespace": "../default"},
        )


def test_dry_run_requires_kubectl(tmp_path):
    with pytest.raises(CapiPivotError, match="KUBECTL_NOT_INSTALLED"):
        _execute(tmp_path, lookup=lambda name: None)


def test_dry_run_requires_clusterctl(tmp_path):
    def lookup(name):
        return "/usr/bin/kubectl" if name == "kubectl" else None

    with pytest.raises(CapiPivotError, match="CLUSTERCTL_NOT_INSTALLED"):
        _execute(tmp_path, lookup=lookup)


def test_dry_run_reports_clusterctl_failure_without_exposing_process_output(tmp_path):
    calls, runner = _runner_for(move_returncode=1)

    with pytest.raises(CapiPivotError, match="CAPI_PIVOT_DRY_RUN_FAILED"):
        _execute(tmp_path, runner=runner)
    assert len(calls) == 2


def test_dry_run_converts_clusterctl_timeout_to_stable_error(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if command[0].endswith("kubectl"):
            return subprocess.CompletedProcess(
                command, 0, _cluster_list("target-cluster"), ""
            )
        raise subprocess.TimeoutExpired(command, 60)

    with pytest.raises(CapiPivotError, match="CAPI_PIVOT_DRY_RUN_TIMEOUT"):
        _execute(tmp_path, runner=runner)
    assert len(calls) == 2


def test_dry_run_rejects_unexpected_payload_fields(tmp_path):
    with pytest.raises(CapiPivotError, match="CAPI_PIVOT_PAYLOAD_FIELD_NOT_ALLOWED"):
        _execute(
            tmp_path,
            payload={"cluster_name": "target-cluster", "shell": "not-allowed"},
        )


def test_dry_run_rejects_same_source_and_target_kubeconfig(tmp_path):
    env = _environment(tmp_path)
    env["CAPI_PIVOT_TARGET_KUBECONFIG"] = env["CAPI_PIVOT_SOURCE_KUBECONFIG"]

    with pytest.raises(CapiPivotError, match="CAPI_PIVOT_KUBECONFIGS_MUST_DIFFER"):
        run_capi_pivot_dry_run(
            {"cluster_name": "target-cluster"},
            environ=env,
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=lambda *args, **kwargs: None,
        )

def test_eph_agent_dispatches_pivot_dry_run(monkeypatch):
    from node_agent import agent

    monkeypatch.setattr(
        agent,
        "run_capi_pivot_dry_run",
        lambda payload: {"dry_run_passed": True, "cluster_name": payload["cluster_name"]},
    )

    result = agent.execute_command(
        {
            "command_type": "cluster.capi.pivot.dry-run",
            "payload": {"cluster_name": "target-cluster"},
        },
        "eph_node",
    )

    assert result == {"dry_run_passed": True, "cluster_name": "target-cluster"}


def test_target_agent_cannot_execute_source_pivot_dry_run():
    from node_agent.agent import NodeAgentError, execute_command

    with pytest.raises(NodeAgentError, match="CAPABILITY_NOT_CONFIGURED"):
        execute_command(
            {
                "command_type": "cluster.capi.pivot.dry-run",
                "payload": {"cluster_name": "target-cluster"},
            },
            "target_cluster",
        )




def test_pivot_execution_runs_dry_run_before_mutating_move(tmp_path):
    calls, runner = _pivot_runner_for()
    result = run_capi_pivot(
        {"cluster_name": "target-cluster", "namespace": "metal3"},
        environ=_environment(tmp_path),
        executable_lookup=lambda name: f"/usr/bin/{name}",
        process_runner=runner,
    )

    assert calls[1][0][1:3] == ["move", "--dry-run"]
    move_calls = [item for item in calls if item[0][0].endswith("clusterctl")]
    assert len(move_calls) == 2
    assert "--dry-run" in move_calls[0][0]
    assert "--dry-run" not in move_calls[1][0]
    assert move_calls[1][1]["timeout"] == 1800
    assert result["pivot_executed"] is True
    assert result["dry_run_passed"] is True
    assert result["side_effects"] is True
    assert result["automatic_retry_allowed"] is False
    assert result["target_management_plane_verified"] is True
    assert result["target_controllers_ready"] is True
    assert result["metal3_handoff"]["all_associated_bmhs_stable"] is True
    assert result["metal3_handoff"]["source_ironic_handoff"] == "deployment_scaled_to_zero"
    assert (
        result["metal3_handoff"]["target_ironic_handoff"]
        == "deployment_ready_with_keepalived"
    )


def test_pivot_execution_does_not_mutate_when_dry_run_fails(tmp_path):
    calls, runner = _runner_for(move_returncode=1)

    with pytest.raises(CapiPivotError, match="CAPI_PIVOT_DRY_RUN_FAILED"):
        run_capi_pivot(
            {"cluster_name": "target-cluster", "namespace": "metal3"},
            environ=_environment(tmp_path),
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=runner,
        )

    assert len(calls) == 2


def test_pivot_execution_timeout_reports_unknown_outcome_without_retry(tmp_path):
    calls, base_runner = _pivot_runner_for()

    def runner(command, **kwargs):
        if command[0].endswith("clusterctl") and "--dry-run" not in command:
            calls.append((command, kwargs))
            raise subprocess.TimeoutExpired(command, 1800)
        return base_runner(command, **kwargs)

    with pytest.raises(
        CapiPivotError,
        match="CAPI_PIVOT_EXECUTION_TIMEOUT_OUTCOME_UNKNOWN",
    ):
        run_capi_pivot(
            {"cluster_name": "target-cluster", "namespace": "metal3"},
            environ=_environment(tmp_path),
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=runner,
        )

    assert len([item for item in calls if item[0][0].endswith("clusterctl")]) == 2



def test_pivot_execution_stops_when_target_controller_is_not_ready(tmp_path):
    calls, runner = _pivot_runner_for(target_controller_ready=False)

    with pytest.raises(
        CapiPivotError,
        match="CAPI_PIVOT_TARGET_CONTROLLERS_NOT_READY",
    ):
        run_capi_pivot(
            {"cluster_name": "target-cluster", "namespace": "metal3"},
            environ=_environment(tmp_path),
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=runner,
        )

    assert len([item for item in calls if item[0][0].endswith("clusterctl")]) == 1


def test_pivot_execution_rejects_bmh_in_transient_provisioning_state(tmp_path):
    bmh = {
        "metadata": {
            "name": "worker-01",
            "namespace": "metal3",
            "uid": "bmh-uid-01",
            "annotations": {},
            "labels": {},
        },
        "spec": {"consumerRef": {"kind": "Metal3Machine", "name": "worker-01"}},
        "status": {
            "operationalStatus": "OK",
            "provisioning": {"state": "provisioning"},
        },
    }
    calls, runner = _pivot_runner_for(bmh_items=[bmh])

    with pytest.raises(
        CapiPivotError,
        match="CAPI_PIVOT_SOURCE_BMH_NOT_SAFE",
    ):
        run_capi_pivot(
            {"cluster_name": "target-cluster", "namespace": "metal3"},
            environ=_environment(tmp_path),
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=runner,
        )

    assert not any(
        item[0][0].endswith("clusterctl") and "--dry-run" not in item[0]
        for item in calls
    )


def test_pivot_execution_rejects_source_ironic_still_running(tmp_path):
    calls, runner = _pivot_runner_for(source_ironic_replicas=1)

    with pytest.raises(
        CapiPivotError,
        match="CAPI_PIVOT_SOURCE_IRONIC_MUST_BE_STOPPED",
    ):
        run_capi_pivot(
            {"cluster_name": "target-cluster", "namespace": "metal3"},
            environ=_environment(tmp_path),
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=runner,
        )

    assert not any(
        item[0][0].endswith("clusterctl") and "--dry-run" not in item[0]
        for item in calls
    )


def test_pivot_execution_rejects_target_ironic_without_keepalived(tmp_path):
    calls, runner = _pivot_runner_for(target_keepalived=False)

    with pytest.raises(
        CapiPivotError,
        match="CAPI_PIVOT_TARGET_IRONIC_KEEPALIVED_MISSING",
    ):
        run_capi_pivot(
            {"cluster_name": "target-cluster", "namespace": "metal3"},
            environ=_environment(tmp_path),
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=runner,
        )

    assert not any(
        item[0][0].endswith("clusterctl") and "--dry-run" not in item[0]
        for item in calls
    )


def _postflight_runner(annotation_present=False, include_bmh=True):
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        resource = command[command.index("get") + 1]
        if resource == "clusters.cluster.x-k8s.io":
            document = {"metadata": {"name": "target-cluster", "uid": "cluster-uid"}}
        elif resource == "machines.cluster.x-k8s.io":
            document = {"items": [{
                "metadata": {
                    "name": "worker-01",
                    "labels": {"cluster.x-k8s.io/cluster-name": "target-cluster"},
                }
            }]}
        elif resource == "metal3machines.infrastructure.cluster.x-k8s.io":
            document = {"items": [{
                "metadata": {
                    "name": "worker-01",
                    "labels": {"cluster.x-k8s.io/cluster-name": "target-cluster"},
                    "annotations": {"metal3.io/BareMetalHost": "worker-01"},
                }
            }]}
        elif resource == "baremetalhosts.metal3.io":
            hosts = []
            if include_bmh:
                annotations = {}
                if annotation_present:
                    annotations["baremetalhost.metal3.io/status"] = (
                        '{"provisioning":{"state":"provisioned"}}'
                    )
                hosts = [{
                    "metadata": {
                        "name": "worker-01",
                        "namespace": "metal3",
                        "annotations": annotations,
                    },
                    "status": {
                        "provisioning": {"state": "provisioned"},
                        "operationalStatus": "OK",
                    },
                }]
            document = {"items": hosts}
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(document), "")

    return calls, runner


def test_pivot_postflight_verifies_reconstructed_bmh_status(tmp_path):
    calls, runner = _postflight_runner()
    result = run_capi_pivot_postflight(
        {
            "cluster_name": "target-cluster",
            "namespace": "metal3",
            "baremetal_host_namespace": "metal3-hosts",
        },
        environ=_environment(tmp_path),
        executable_lookup=lambda name: f"/usr/bin/{name}",
        process_runner=runner,
    )

    assert result["postflight_passed"] is True
    assert result["side_effects"] is False
    assert result["verified_bmh_count"] == 1
    assert result["bmh_status_annotations_consumed"] is True
    assert result["verified_bmhs"][0]["baremetalhost"] == "worker-01"
    assert len(calls) == 4
    bmh_command = next(command for command in calls if "baremetalhosts.metal3.io" in command)
    assert bmh_command[bmh_command.index("--namespace") + 1] == "metal3-hosts"


def test_pivot_postflight_rejects_unconsumed_bmh_status_annotation(tmp_path):
    _, runner = _postflight_runner(annotation_present=True)

    with pytest.raises(
        CapiPivotError,
        match="CAPI_PIVOT_POSTFLIGHT_BMH_STATUS_RECONSTRUCTION_PENDING",
    ):
        run_capi_pivot_postflight(
            {"cluster_name": "target-cluster", "namespace": "metal3"},
            environ=_environment(tmp_path),
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=runner,
        )


def test_pivot_postflight_rejects_missing_bmh_reference(tmp_path):
    _, runner = _postflight_runner(include_bmh=False)

    with pytest.raises(
        CapiPivotError,
        match="CAPI_PIVOT_POSTFLIGHT_BMH_NOT_FOUND",
    ):
        run_capi_pivot_postflight(
            {"cluster_name": "target-cluster", "namespace": "metal3"},
            environ=_environment(tmp_path),
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=runner,
        )
