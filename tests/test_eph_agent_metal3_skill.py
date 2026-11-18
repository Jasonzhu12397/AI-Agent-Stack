import json
from threading import Event

import pytest

from apps.eph_agent.metal3_skill import (
    Metal3SkillError,
    apply_baremetalhosts,
    apply_capi_cluster_resources,
    observe_baremetalhosts,
    observe_capi_cluster,
)


def _bmh_manifest():
    return {
        "apiVersion": "metal3.io/v1alpha1",
        "kind": "BareMetalHost",
        "metadata": {"name": "worker-01", "namespace": "metal3"},
        "spec": {
            "online": False,
            "bootMACAddress": "aa:bb:cc:dd:ee:ff",
            "bmc": {
                "address": "redfish://192.0.2.10/redfish/v1/Systems/1",
                "credentialsName": "worker-01-bmc-secret",
                "disableCertificateVerification": True,
            },
        },
    }


def test_bmh_registration_uses_existing_secret_reference_and_server_side_apply():
    calls = []

    def runner(arguments, timeout, max_output_bytes):
        calls.append((arguments, timeout, max_output_bytes))
        return {"available": True, "returncode": 0, "stdout": "configured", "stderr": ""}

    result = apply_baremetalhosts(
        {"namespace": "metal3", "manifests": [_bmh_manifest()]},
        runner,
    )

    assert result["server_side_dry_run_passed"] is True
    assert result["server_side_apply_succeeded"] is True
    assert result["resource_names"] == ["worker-01"]
    assert len(calls) == 2
    assert "--dry-run=server" in calls[0][0]
    assert "--server-side" in calls[1][0]


def test_bmh_registration_rejects_credentials_embedded_in_bmc_url():
    manifest = _bmh_manifest()
    manifest["spec"]["bmc"]["address"] = (
        "redfish://operator:password@192.0.2.10/redfish/v1/Systems/1"
    )

    with pytest.raises(Metal3SkillError, match="METAL3_BMH_BMC_CREDENTIAL_REFERENCE_INVALID"):
        apply_baremetalhosts(
            {"namespace": "metal3", "manifests": [manifest]},
            lambda *_args, **_kwargs: {
                "available": True,
                "returncode": 0,
                "stdout": "",
                "stderr": "",
            },
        )


def test_bmh_registration_rejects_secret_objects_and_cross_namespace_resources():
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "bmc", "namespace": "metal3"},
        "data": {"password": "not-allowed"},
    }
    with pytest.raises(Metal3SkillError, match="METAL3_RESOURCE_KIND_NOT_ALLOWED"):
        apply_baremetalhosts(
            {"namespace": "metal3", "manifests": [secret]},
            lambda *_args, **_kwargs: {},
        )

    manifest = _bmh_manifest()
    manifest["metadata"]["namespace"] = "kube-system"
    with pytest.raises(Metal3SkillError, match="METAL3_RESOURCE_IDENTITY_INVALID"):
        apply_baremetalhosts(
            {"namespace": "metal3", "manifests": [manifest]},
            lambda *_args, **_kwargs: {},
        )


def test_capi_apply_is_limited_to_capi_and_metal3_resources():
    cluster = {
        "apiVersion": "cluster.x-k8s.io/v1beta2",
        "kind": "Cluster",
        "metadata": {"name": "target-01", "namespace": "metal3"},
        "spec": {
            "controlPlaneRef": {
                "apiGroup": "controlplane.cluster.x-k8s.io",
                "kind": "KubeadmControlPlane",
                "name": "target-01-control-plane",
            }
        },
    }
    calls = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        return {"available": True, "returncode": 0, "stdout": "applied", "stderr": ""}

    result = apply_capi_cluster_resources(
        {"namespace": "metal3", "manifests": [cluster]},
        runner,
    )

    assert result["resource_type"] == "CAPI_CLUSTER"
    assert result["resource_names"] == ["target-01"]
    assert len(calls) == 2


def test_capi_apply_rejects_arbitrary_cluster_scoped_resources():
    manifest = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": "attacker-controlled"},
    }
    with pytest.raises(Metal3SkillError, match="METAL3_RESOURCE_KIND_NOT_ALLOWED"):
        apply_capi_cluster_resources(
            {"namespace": "metal3", "manifests": [manifest]},
            lambda *_args, **_kwargs: {},
        )


def test_server_side_dry_run_failure_prevents_mutation():
    calls = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        return {
            "available": True,
            "returncode": 1,
            "stdout": "",
            "stderr": "admission rejected",
        }

    with pytest.raises(Metal3SkillError) as error:
        apply_baremetalhosts(
            {"namespace": "metal3", "manifests": [_bmh_manifest()]},
            runner,
        )

    assert error.value.side_effects is False
    assert len(calls) == 1
    assert "--dry-run=server" in calls[0]


def test_apply_failure_reports_possible_partial_side_effects():
    calls = []

    def runner(arguments, **_kwargs):
        calls.append(arguments)
        if "--dry-run=server" in arguments:
            return {"available": True, "returncode": 0, "stdout": "", "stderr": ""}
        return {
            "available": True,
            "returncode": 1,
            "stdout": "",
            "stderr": "partial apply",
        }

    with pytest.raises(Metal3SkillError) as error:
        apply_baremetalhosts(
            {"namespace": "metal3", "manifests": [_bmh_manifest()]},
            runner,
        )

    assert error.value.side_effects is True
    assert len(calls) == 2


def test_lease_loss_blocks_before_any_metal3_mutation():
    lease_lost = Event()
    lease_lost.set()
    calls = []

    with pytest.raises(Metal3SkillError, match="COMMAND_LEASE_LOST_BEFORE_METAL3_APPLY"):
        apply_baremetalhosts(
            {"namespace": "metal3", "manifests": [_bmh_manifest()]},
            lambda *args, **kwargs: calls.append((args, kwargs)),
            lease_lost,
        )

    assert calls == []


def test_bmh_observer_reports_hardware_and_ironics_provisioning_state():
    inventory = {
        "items": [{
            "metadata": {"name": "worker-01", "namespace": "metal3"},
            "spec": {
                "online": True,
                "bootMACAddress": "aa:bb:cc:dd:ee:ff",
            },
            "status": {
                "operationalStatus": "OK",
                "provisioning": {"state": "available"},
                "hardware": {
                    "systemUUID": "11111111-2222-3333-4444-555555555555",
                    "systemSerialNumber": "SERIAL-01",
                    "cpu": {"count": 32},
                    "ramMebibytes": 131072,
                    "nics": [{"name": "eno1"}],
                    "storage": [{"name": "disk0"}],
                },
            },
        }]
    }

    result = observe_baremetalhosts(
        {"namespace": "metal3"},
        lambda *_args, **_kwargs: {
            "available": True,
            "returncode": 0,
            "stdout": json.dumps(inventory),
            "stderr": "",
        },
    )

    assert result["host_count"] == 1
    assert result["available_count"] == 1
    assert result["hosts"][0]["hardware"]["ram_mib"] == 131072


def test_capi_cluster_observer_requires_control_plane_ready_condition():
    cluster = {
        "metadata": {"name": "target-01", "uid": "cluster-uid"},
        "status": {
            "observedGeneration": 4,
            "conditions": [
                {"type": "Ready", "status": "True"},
                {"type": "ControlPlaneReady", "status": "True"},
            ],
        },
    }

    result = observe_capi_cluster(
        {"namespace": "metal3", "cluster_name": "target-01"},
        lambda *_args, **_kwargs: {
            "available": True,
            "returncode": 0,
            "stdout": json.dumps(cluster),
            "stderr": "",
        },
    )

    assert result["control_plane_ready"] is True
    assert result["cluster_uid"] == "cluster-uid"
