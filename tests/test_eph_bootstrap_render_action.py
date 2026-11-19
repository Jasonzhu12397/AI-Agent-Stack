"""The EPH bootstrap render action must be pure and reproducible.

The renderer in ``apps.eph_agent.bootstrap_renderer`` used to be unused; wiring
it into the EPH execute loop as ``eph.bootstrap.render`` lets the control plane
ask for the kubeadm / cloud-init / agent manifests without any cluster write.
"""

from apps.eph_agent.main import execute


def _spec(**overrides: object) -> dict:
    spec: dict = {
        "node_name": "eph-bootstrap-01",
        "kubernetes_version": "v1.31.2",
        "node_ip": "192.0.2.10",
        "pod_cidr": "10.244.0.0/16",
        "service_cidr": "10.96.0.0/12",
        "control_plane_endpoint": "192.0.2.10",
        "control_plane_port": 6443,
        "ntp_servers": ["192.0.2.1"],
        "agent": {
            "control_endpoint": "https://cloud-control.example.test",
            "image": "oci://registry.example.test/eph-agent:v1",
            "mtls_secret_ref": "eph-agent-mtls",
            "poll_seconds": 15,
            "agent_id": "0b8a5dc2-6f6f-4b4e-9a4c-3f6a1b7c8d90",
            "installation_id": "1f0c2a44-8b1d-4c3e-8f5a-2b6c9d0e1f22",
        },
    }
    spec.update(overrides)
    return spec


def _render(spec: dict) -> dict:
    return execute({"action": "eph.bootstrap.render", "payload": {"spec": spec}})


def test_bootstrap_render_returns_success_without_side_effects():
    result = _render(_spec())

    assert result["status"] == "succeeded"
    assert result["error_code"] is None
    assert result["side_effects"] is False
    assert result["output"]["resource_type"] == "EPH_BOOTSTRAP"
    assert result["output"]["artifact_count"] == 3
    assert set(result["output"]["artifact_digests"]) == {
        "kubeadm_init",
        "cloud_init_seed",
        "agent_deployment",
    }


def test_bootstrap_render_is_reproducible_for_the_same_spec():
    first = _render(_spec())
    second = _render(_spec())

    assert first["output"]["artifact_digests"] == second["output"]["artifact_digests"]


def test_bootstrap_render_digest_changes_when_spec_changes():
    baseline = _render(_spec())
    changed = _render(_spec(node_ip="192.0.2.11", control_plane_endpoint="192.0.2.11"))

    assert (
        baseline["output"]["artifact_digests"]["kubeadm_init"]
        != changed["output"]["artifact_digests"]["kubeadm_init"]
    )


def test_bootstrap_render_accepts_the_spec_as_the_payload_itself():
    result = execute({"action": "eph.bootstrap.render", "payload": _spec()})

    assert result["status"] == "succeeded"
    assert result["output"]["artifact_count"] == 3


def test_bootstrap_render_rejects_invalid_spec_with_render_error_code():
    result = execute(
        {
            "action": "eph.bootstrap.render",
            "payload": {"spec": _spec(node_name="Invalid Node")},
        }
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "EPH_BOOTSTRAP_NODE_NAME_INVALID"
    assert result["side_effects"] is False


def test_bootstrap_render_rejects_non_mapping_payload():
    result = execute({"action": "eph.bootstrap.render", "payload": ["not-a-spec"]})

    assert result["status"] == "failed"
    assert result["error_code"] == "COMMAND_PAYLOAD_INVALID"
