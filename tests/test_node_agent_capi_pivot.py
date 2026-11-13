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
    target_generation=1,
    target_observed_generation=1,
    source_ironic_observed_generation=1,
    source_ironic_status_replicas=0,
    source_ironic_ready_replicas=0,
    source_ironic_updated_replicas=0,
    source_ironic_terminating_replicas=0,
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
                            "metadata": {"generation": 1},
                            "spec": {"replicas": source_ironic_replicas},
                            "status": {
                                "observedGeneration": source_ironic_observed_generation,
                                "replicas": source_ironic_status_replicas,
                                "readyReplicas": source_ironic_ready_replicas,
                                "updatedReplicas": source_ironic_updated_replicas,
                                "availableReplicas": source_ironic_replicas,
                                "terminatingReplicas": source_ironic_terminating_replicas,
                            },
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
                        "metadata": {"generation": target_generation},
                        "spec": {
                            "replicas": 1,
                            "template": {"spec": {"containers": containers}},
                        },
                        "status": {
                            "availableReplicas": 1,
                            "observedGeneration": target_observed_generation,
                        },
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




def test_pivot_execution_runs_read_only_preflight_and_simulates_handoff(tmp_path):
    calls, runner = _pivot_runner_for()
    result = run_capi_pivot(
        {"cluster_name": "target-cluster", "namespace": "metal3"},
        environ=_environment(tmp_path),
        executable_lookup=lambda name: f"/usr/bin/{name}",
        process_runner=runner,
    )

    move_calls = [item for item in calls if item[0][0].endswith("clusterctl")]
    assert len(move_calls) == 1
    assert "--dry-run" in move_calls[0][0]
    assert result["pivot_executed"] is False
    assert result["pivot_simulated"] is True
    assert result["execution_mode"] == "preflight_verified_simulation"
    assert result["simulation_status"] == "ready_for_target_agent_acknowledgement"
    assert result["dry_run_passed"] is True
    assert result["side_effects"] is False
    assert result["ownership_transferred"] is False
    assert result["target_agent_acknowledgement_required"] is True
    assert result["postflight_required"] is True
    assert result["automatic_retry_allowed"] is True
    assert result["target_management_plane_verified"] is True
    assert result["target_controllers_ready"] is True
    assert result["metal3_handoff"]["all_associated_bmhs_stable"] is True
    assert (
        result["metal3_handoff"]["source_ironic_handoff"]
        == "deployment_scaled_to_zero_and_observed"
    )
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


def test_pivot_execution_never_starts_mutating_move_after_dry_run(tmp_path):
    calls, runner = _pivot_runner_for()
    result = run_capi_pivot(
        {"cluster_name": "target-cluster", "namespace": "metal3"},
        environ=_environment(tmp_path),
        executable_lookup=lambda name: f"/usr/bin/{name}",
        process_runner=runner,
    )

    clusterctl_calls = [item for item in calls if item[0][0].endswith("clusterctl")]
    assert len(clusterctl_calls) == 1
    assert clusterctl_calls[0][0][1:3] == ["move", "--dry-run"]
    assert result["pivot_simulated"] is True
    assert result["side_effects"] is False

@pytest.mark.parametrize(
    ("malformed_response", "error_code"),
    [
        ("api_groups", "CAPI_PIVOT_TARGET_API_DISCOVERY_FAILED"),
        ("api_resources", "CAPI_PIVOT_TARGET_API_DISCOVERY_FAILED"),
        ("controller", "CAPI_PIVOT_TARGET_CONTROLLERS_NOT_READY"),
        ("cluster_inventory", "CAPI_PIVOT_TARGET_CLUSTER_CHECK_FAILED"),
    ],
)
def test_pivot_preflight_rejects_malformed_target_api_responses(
    tmp_path, malformed_response, error_code
):
    _, base_runner = _pivot_runner_for()

    def runner(command, **kwargs):
        if command[0].endswith("kubectl"):
            if "--raw" in command:
                raw_path = command[command.index("--raw") + 1]
                if malformed_response == "api_groups" and raw_path == "/apis":
                    return subprocess.CompletedProcess(command, 0, "[]", "")
                if (
                    malformed_response == "api_resources"
                    and raw_path.startswith("/apis/cluster.x-k8s.io/")
                ):
                    return subprocess.CompletedProcess(
                        command, 0, '{"resources": "invalid"}', ""
                    )
            if (
                malformed_response == "controller"
                and "deployment" in command
                and command[command.index("deployment") + 1]
                == "capi-controller-manager"
            ):
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({
                        "metadata": {"generation": 1},
                        "spec": [],
                        "status": {"availableReplicas": 1, "observedGeneration": 1},
                    }),
                    "",
                )
            if (
                malformed_response == "cluster_inventory"
                and "clusters.cluster.x-k8s.io" in command
            ):
                return subprocess.CompletedProcess(
                    command, 0, '{"items": [{"metadata": []}]}', ""
                )
        return base_runner(command, **kwargs)

    with pytest.raises(CapiPivotError, match=error_code):
        run_capi_pivot(
            {"cluster_name": "target-cluster", "namespace": "metal3"},
            environ=_environment(tmp_path),
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=runner,
        )


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


@pytest.mark.parametrize(
    ("mutation", "expected_blocker"),
    [
        ("missing_move_label", "bmh_clusterctl_move_label_missing"),
        ("missing_pause_annotation", "bmh_pause_annotation_missing"),
        ("invalid_pause_annotation", "bmh_pause_annotation_value_invalid"),
        ("missing_status_annotation", "bmh_status_annotation_missing"),
        ("mismatched_status_annotation", "bmh_status_annotation_mismatch"),
        ("block_move", "bmh_block_move_annotation_present"),
    ],
)
def test_pivot_execution_requires_metal3_bmh_handoff_metadata(
    tmp_path,
    mutation,
    expected_blocker,
):
    status = {
        "operationalStatus": "OK",
        "provisioning": {"state": "provisioned"},
    }
    annotations = {
        "baremetalhost.metal3.io/paused": "metal3.io/capm3",
        "baremetalhost.metal3.io/status": json.dumps(status),
    }
    labels = {"clusterctl.cluster.x-k8s.io/move": ""}
    if mutation == "missing_pause_annotation":
        annotations.pop("baremetalhost.metal3.io/paused")
    elif mutation == "invalid_pause_annotation":
        annotations["baremetalhost.metal3.io/paused"] = "true"
    elif mutation == "missing_status_annotation":
        annotations.pop("baremetalhost.metal3.io/status")
    elif mutation == "mismatched_status_annotation":
        annotations["baremetalhost.metal3.io/status"] = "{"
    elif mutation == "block_move":
        annotations["clusterctl.cluster.x-k8s.io/block-move"] = ""
    elif mutation == "missing_move_label":
        labels = {}
    bmh = {
        "metadata": {
            "name": "worker-01",
            "namespace": "metal3",
            "uid": "bmh-uid-01",
            "annotations": annotations,
            "labels": labels,
        },
        "spec": {
            "consumerRef": {
                "kind": "Metal3Machine",
                "name": "worker-01",
            },
        },
        "status": status,
    }
    calls, runner = _pivot_runner_for(bmh_items=[bmh])

    with pytest.raises(
        CapiPivotError,
        match="CAPI_PIVOT_SOURCE_BMH_NOT_SAFE",
    ) as error:
        run_capi_pivot(
            {"cluster_name": "target-cluster", "namespace": "metal3"},
            environ=_environment(tmp_path),
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=runner,
        )

    assert expected_blocker in str(error.value)
    assert not any(
        item[0][0].endswith("clusterctl") and "--dry-run" not in item[0]
        for item in calls
    )


def test_pivot_execution_accepts_fully_prepared_metal3_bmh(tmp_path):
    status = {
        "operationalStatus": "OK",
        "provisioning": {"state": "provisioned"},
        "hardware": {
            "cpu": {"arch": "x86_64", "count": 32},
            "ramMebibytes": 262144,
        },
    }
    bmh = {
        "metadata": {
            "name": "worker-01",
            "namespace": "metal3",
            "uid": "bmh-uid-01",
            "annotations": {
                "baremetalhost.metal3.io/paused": "metal3.io/capm3",
                "baremetalhost.metal3.io/status": json.dumps(status),
            },
            "labels": {"clusterctl.cluster.x-k8s.io/move": ""},
        },
        "spec": {
            "consumerRef": {
                "kind": "Metal3Machine",
                "name": "worker-01",
            },
        },
        "status": status,
    }
    calls, runner = _pivot_runner_for(bmh_items=[bmh])
    result = run_capi_pivot(
        {"cluster_name": "target-cluster", "namespace": "metal3"},
        environ=_environment(tmp_path),
        executable_lookup=lambda name: f"/usr/bin/{name}",
        process_runner=runner,
    )

    assert result["pivot_executed"] is False
    assert result["pivot_simulated"] is True
    assert result["side_effects"] is False
    assert result["metal3_handoff"]["associated_bmh_count"] == 1
    assert result["metal3_handoff"]["all_associated_bmhs_stable"] is True
    assert all(
        "--dry-run" in item[0]
        for item in calls
        if item[0][0].endswith("clusterctl")
    )


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


@pytest.mark.parametrize(
    ("overrides", "error_code"),
    [
        (
            {"source_ironic_observed_generation": 0},
            "CAPI_PIVOT_SOURCE_IRONIC_MUST_BE_STOPPED",
        ),
        (
            {"source_ironic_status_replicas": 1},
            "CAPI_PIVOT_SOURCE_IRONIC_MUST_BE_STOPPED",
        ),
        (
            {"source_ironic_ready_replicas": 1},
            "CAPI_PIVOT_SOURCE_IRONIC_MUST_BE_STOPPED",
        ),
        (
            {"source_ironic_updated_replicas": 1},
            "CAPI_PIVOT_SOURCE_IRONIC_MUST_BE_STOPPED",
        ),
        (
            {"source_ironic_terminating_replicas": 1},
            "CAPI_PIVOT_SOURCE_IRONIC_MUST_BE_STOPPED",
        ),
    ],
)
def test_pivot_execution_requires_observed_zero_replica_source_ironic(
    tmp_path, overrides, error_code
):
    _, runner = _pivot_runner_for(**overrides)

    with pytest.raises(CapiPivotError, match=error_code):
        run_capi_pivot(
            {"cluster_name": "target-cluster", "namespace": "metal3"},
            environ=_environment(tmp_path),
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=runner,
        )


@pytest.mark.parametrize(
    ("overrides", "error_code"),
    [
        (
            {"target_observed_generation": 0},
            "CAPI_PIVOT_TARGET_IRONIC_NOT_READY",
        ),
        (
            {"target_generation": True},
            "CAPI_PIVOT_TARGET_IRONIC_NOT_READY",
        ),
    ],
)
def test_pivot_execution_requires_current_target_ironic_deployment_status(
    tmp_path, overrides, error_code
):
    _, runner = _pivot_runner_for(**overrides)

    with pytest.raises(CapiPivotError, match=error_code):
        run_capi_pivot(
            {"cluster_name": "target-cluster", "namespace": "metal3"},
            environ=_environment(tmp_path),
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=runner,
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


def _postflight_runner(
    annotation_present=False,
    include_bmh=True,
    controller_ready=True,
    consumer_ref_valid=True,
    consumer_ref_api_version_valid=True,
    consumer_ref_namespace_valid=True,
):
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        resource = command[command.index("get") + 1]
        if resource == "clusters.cluster.x-k8s.io":
            document = {"metadata": {"name": "target-cluster", "uid": "cluster-uid"}}
        elif resource == "deployment":
            name = command[command.index("deployment") + 1]
            ready = controller_ready or name != "capm3-controller-manager"
            document = {
                "metadata": {"generation": 1},
                "spec": {"replicas": 1},
                "status": {
                    "availableReplicas": 1 if ready else 0,
                    "observedGeneration": 1,
                },
            }
        elif resource == "machines.cluster.x-k8s.io":
            document = {"items": [{
                "metadata": {
                    "name": "worker-01",
                    "uid": "machine-uid",
                    "labels": {"cluster.x-k8s.io/cluster-name": "target-cluster"},
                },
                "spec": {
                    "infrastructureRef": {
                        "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
                        "kind": "Metal3Machine",
                        "name": "worker-01",
                    }
                },
            }]}
        elif resource == "metal3machines.infrastructure.cluster.x-k8s.io":
            document = {"items": [{
                "metadata": {
                    "name": "worker-01",
                    "uid": "metal3machine-uid",
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
                        "uid": "bmh-uid-01",
                        "annotations": annotations,
                    },
                    "spec": {
                        "consumerRef": {
                            "apiVersion": (
                                "infrastructure.cluster.x-k8s.io/v1beta1"
                                if consumer_ref_api_version_valid
                                else "infrastructure.cluster.x-k8s.io/v1beta2"
                            ),
                            "kind": "Metal3Machine",
                            "name": "worker-01",
                            "namespace": (
                                "metal3"
                                if consumer_ref_namespace_valid
                                else "wrong-namespace"
                            ),
                            "uid": (
                                "metal3machine-uid"
                                if consumer_ref_valid
                                else "stale-metal3machine-uid"
                            ),
                        }
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
    assert len(calls) == 8
    bmh_command = next(
        command for command in calls if "baremetalhosts.metal3.io" in command
    )
    assert bmh_command[bmh_command.index("--namespace") + 1] == "metal3-hosts"


@pytest.mark.parametrize(
    ("resource", "field", "value", "error_code"),
    [
        (
            "baremetalhosts.metal3.io",
            "spec",
            [],
            "CAPI_PIVOT_POSTFLIGHT_BMH_SPEC_INVALID",
        ),
        (
            "baremetalhosts.metal3.io",
            "status",
            [],
            "CAPI_PIVOT_POSTFLIGHT_BMH_STATUS_INVALID",
        ),
        (
            "machines.cluster.x-k8s.io",
            "spec",
            [],
            "CAPI_PIVOT_POSTFLIGHT_MACHINE_SPEC_INVALID",
        ),
    ],
)
def test_pivot_postflight_rejects_malformed_resource_objects(
    tmp_path, resource, field, value, error_code
):
    _, base_runner = _postflight_runner()

    def runner(command, **kwargs):
        completed = base_runner(command, **kwargs)
        if resource in command:
            document = json.loads(completed.stdout)
            document["items"][0][field] = value
            return subprocess.CompletedProcess(
                command, completed.returncode, json.dumps(document), completed.stderr
            )
        return completed

    with pytest.raises(CapiPivotError, match=error_code):
        run_capi_pivot_postflight(
            {"cluster_name": "target-cluster", "namespace": "metal3"},
            environ=_environment(tmp_path),
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=runner,
        )


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

def test_pivot_postflight_rejects_unready_provider_controller(tmp_path):
    _, runner = _postflight_runner(controller_ready=False)

    with pytest.raises(
        CapiPivotError,
        match="CAPI_PIVOT_POSTFLIGHT_CONTROLLER_NOT_READY",
    ):
        run_capi_pivot_postflight(
            {"cluster_name": "target-cluster", "namespace": "metal3"},
            environ=_environment(tmp_path),
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=runner,
        )


def test_pivot_postflight_rejects_stale_bmh_consumer_identity(tmp_path):
    _, runner = _postflight_runner(consumer_ref_valid=False)

    with pytest.raises(
        CapiPivotError,
        match="CAPI_PIVOT_POSTFLIGHT_BMH_CONSUMER_REFERENCE_MISMATCH",
    ):
        run_capi_pivot_postflight(
            {"cluster_name": "target-cluster", "namespace": "metal3"},
            environ=_environment(tmp_path),
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=runner,
        )


@pytest.mark.parametrize(
    ("override", "error_code"),
    [
        (
            {"consumer_ref_api_version_valid": False},
            "CAPI_PIVOT_POSTFLIGHT_BMH_CONSUMER_REFERENCE_MISMATCH",
        ),
        (
            {"consumer_ref_namespace_valid": False},
            "CAPI_PIVOT_POSTFLIGHT_BMH_CONSUMER_REFERENCE_MISMATCH",
        ),
    ],
)
def test_pivot_postflight_rejects_bmh_consumer_reference_scope_mismatch(
    tmp_path, override, error_code
):
    _, runner = _postflight_runner(**override)

    with pytest.raises(CapiPivotError, match=error_code):
        run_capi_pivot_postflight(
            {"cluster_name": "target-cluster", "namespace": "metal3"},
            environ=_environment(tmp_path),
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=runner,
        )


def test_pivot_postflight_matches_each_machine_to_its_own_infrastructure_ref(tmp_path):
    _, base_runner = _postflight_runner()

    def runner(command, **kwargs):
        completed = base_runner(command, **kwargs)
        resource = command[command.index("get") + 1]
        document = json.loads(completed.stdout)
        if resource == "machines.cluster.x-k8s.io":
            second = json.loads(json.dumps(document["items"][0]))
            second["metadata"]["name"] = "worker-02"
            second["metadata"]["uid"] = "machine-uid-02"
            second["spec"]["infrastructureRef"]["name"] = "worker-02"
            document["items"].append(second)
        elif resource == "metal3machines.infrastructure.cluster.x-k8s.io":
            second = json.loads(json.dumps(document["items"][0]))
            second["metadata"]["name"] = "worker-02"
            second["metadata"]["uid"] = "metal3machine-uid-02"
            second["metadata"]["annotations"]["metal3.io/BareMetalHost"] = "worker-02"
            document["items"].append(second)
        elif resource == "baremetalhosts.metal3.io":
            second = json.loads(json.dumps(document["items"][0]))
            second["metadata"]["name"] = "worker-02"
            second["metadata"]["uid"] = "bmh-uid-02"
            second["spec"]["consumerRef"]["name"] = "worker-02"
            second["spec"]["consumerRef"]["uid"] = "metal3machine-uid-02"
            document["items"].append(second)
        return subprocess.CompletedProcess(
            command, completed.returncode, json.dumps(document), completed.stderr
        )

    result = run_capi_pivot_postflight(
        {"cluster_name": "target-cluster", "namespace": "metal3"},
        environ=_environment(tmp_path),
        executable_lookup=lambda name: f"/usr/bin/{name}",
        process_runner=runner,
    )

    assert result["postflight_passed"] is True
    assert result["verified_machine_count"] == 2
    assert result["metal3machine_count"] == 2
    assert result["verified_bmh_count"] == 2


def test_pivot_postflight_rejects_malformed_machine_cluster_labels(tmp_path):
    _, base_runner = _postflight_runner()

    def runner(command, **kwargs):
        completed = base_runner(command, **kwargs)
        if "machines.cluster.x-k8s.io" in command:
            document = json.loads(completed.stdout)
            document["items"][0]["metadata"]["labels"] = []
            return subprocess.CompletedProcess(
                command, 0, json.dumps(document), completed.stderr
            )
        return completed

    with pytest.raises(
        CapiPivotError,
        match="CAPI_PIVOT_POSTFLIGHT_MACHINE_LABELS_INVALID",
    ):
        run_capi_pivot_postflight(
            {"cluster_name": "target-cluster", "namespace": "metal3"},
            environ=_environment(tmp_path),
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=runner,
        )


def test_pivot_postflight_rejects_duplicate_bmh_identity(tmp_path):
    _, base_runner = _postflight_runner()

    def runner(command, **kwargs):
        completed = base_runner(command, **kwargs)
        if "baremetalhosts.metal3.io" in command:
            document = json.loads(completed.stdout)
            document["items"].append(json.loads(json.dumps(document["items"][0])))
            return subprocess.CompletedProcess(
                command, 0, json.dumps(document), completed.stderr
            )
        return completed

    with pytest.raises(
        CapiPivotError,
        match="CAPI_PIVOT_POSTFLIGHT_BMH_IDENTITY_INVALID",
    ):
        run_capi_pivot_postflight(
            {"cluster_name": "target-cluster", "namespace": "metal3"},
            environ=_environment(tmp_path),
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=runner,
        )



def test_pivot_execution_cancellation_before_mutation_stops_clusterctl_move(tmp_path):
    import threading

    cancellation_event = threading.Event()
    calls, base_runner = _pivot_runner_for()

    def runner(command, **kwargs):
        if command[0].endswith("clusterctl") and "--dry-run" in command:
            cancellation_event.set()
        return base_runner(command, **kwargs)

    with pytest.raises(
        CapiPivotError,
        match="CAPI_PIVOT_CANCELLED_DURING_PREFLIGHT",
    ):
        run_capi_pivot(
            {"cluster_name": "target-cluster", "namespace": "metal3"},
            environ=_environment(tmp_path),
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=runner,
            cancellation_event=cancellation_event,
        )

    assert not any(
        item[0][0].endswith("clusterctl") and "--dry-run" not in item[0]
        for item in calls
    )


def test_pivot_preflight_stops_after_lease_loss_during_source_inspection(tmp_path):
    import threading

    cancellation_event = threading.Event()
    calls, base_runner = _pivot_runner_for()

    def runner(command, **kwargs):
        completed = base_runner(command, **kwargs)
        if command[0].endswith("kubectl"):
            cancellation_event.set()
        return completed

    with pytest.raises(
        CapiPivotError,
        match="CAPI_PIVOT_CANCELLED_DURING_PREFLIGHT",
    ):
        run_capi_pivot(
            {"cluster_name": "target-cluster", "namespace": "metal3"},
            environ=_environment(tmp_path),
            executable_lookup=lambda name: f"/usr/bin/{name}",
            process_runner=runner,
            cancellation_event=cancellation_event,
        )

    assert not any(item[0][0].endswith("clusterctl") for item in calls)


def test_pivot_execution_never_invokes_clusterctl_move_without_dry_run(tmp_path):
    calls, runner = _pivot_runner_for()
    result = run_capi_pivot(
        {"cluster_name": "target-cluster", "namespace": "metal3"},
        environ=_environment(tmp_path),
        executable_lookup=lambda name: f"/usr/bin/{name}",
        process_runner=runner,
    )

    clusterctl_calls = [item for item in calls if item[0][0].endswith("clusterctl")]
    assert len(clusterctl_calls) == 1
    assert all("--dry-run" in item[0] for item in clusterctl_calls)
    assert result["pivot_simulated"] is True
    assert result["side_effects"] is False
