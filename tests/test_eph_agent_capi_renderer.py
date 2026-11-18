import pytest

from apps.eph_agent.capi_renderer import (
    render_bmh_manifests,
    render_capi_metal3_manifests,
)
from apps.eph_agent.metal3_skill import Metal3SkillError


def test_bmh_template_renders_secret_reference_without_credentials():
    manifests = render_bmh_manifests(
        [{
            "name": "worker-01",
            "node_pool_name": "high-throughput",
            "bmc_address": "redfish://192.0.2.10/redfish/v1/Systems/1",
            "boot_mac_address": "aa:bb:cc:dd:ee:ff",
            "secret_ref": "worker-01-bmc-secret",
            "online": False,
        }],
        "metal3",
    )

    assert len(manifests) == 1
    assert manifests[0]["kind"] == "BareMetalHost"
    assert manifests[0]["spec"]["bmc"]["credentialsName"] == "worker-01-bmc-secret"
    assert "password" not in str(manifests[0]).lower()


def test_bmh_renderer_rejects_bmc_userinfo_in_url():
    with pytest.raises(Metal3SkillError, match="METAL3_BMC_ADDRESS_INVALID"):
        render_bmh_manifests(
            [{
                "name": "worker-01",
                "node_pool_name": "workers",
                "bmc_address": "redfish://operator:password@192.0.2.10/redfish/v1",
                "boot_mac_address": "aa:bb:cc:dd:ee:ff",
                "secret_ref": "worker-01-bmc-secret",
            }]
        )


def test_capi_template_renders_kubeadm_metal3_control_plane_and_worker_pool():
    manifests = render_capi_metal3_manifests({
        "name": "target-01",
        "namespace": "metal3",
        "infrastructure_provider": "metal3",
        "control_plane_endpoint": "192.0.2.20",
        "control_plane_count": 3,
        "k8s_version": "v1.31.2",
        "image_url": "https://images.example.test/ubuntu-24.04.qcow2",
        "pod_cidr": "10.244.0.0/16",
        "service_cidr": "10.96.0.0/12",
        "control_plane_data_template": {
            "meta_data": {
                "objectNames": [{"key": "hostname", "object": "machine"}],
            },
            "network_data": {
                "links": {
                    "ethernets": [
                        {
                            "type": "phy",
                            "id": "control",
                            "macAddress": {"fromHostInterface": "eno1"},
                        },
                    ],
                },
                "networks": {"ipv4DHCP": [{"id": "control", "link": "control"}]},
            },
        },
        "worker_pools": [{
            "name": "high-throughput",
            "count": 4,
            "node_labels": ["node-type=high-throughput"],
            "data_template": {
                "meta_data": {
                    "objectNames": [{"key": "hostname", "object": "machine"}],
                },
                "network_data": {
                    "links": {
                        "ethernets": [
                            {
                                "type": "phy",
                                "id": "data",
                                "macAddress": {"fromHostInterface": "eno2"},
                            },
                        ],
                    },
                    "networks": {"ipv4DHCP": [{"id": "data", "link": "data"}]},
                },
            },
        }],
    })

    kinds = [item["kind"] for item in manifests]
    assert kinds[:4] == [
        "Cluster",
        "Metal3Cluster",
        "Metal3DataTemplate",
        "Metal3DataTemplate",
    ]
    assert "Metal3MachineTemplate" in kinds
    assert "KubeadmControlPlane" in kinds
    assert "KubeadmConfigTemplate" in kinds
    assert "MachineDeployment" in kinds
    cluster = manifests[0]
    assert cluster["spec"]["controlPlaneRef"]["kind"] == "KubeadmControlPlane"
    assert cluster["spec"]["infrastructureRef"]["kind"] == "Metal3Cluster"
    control_plane = next(item for item in manifests if item["kind"] == "KubeadmControlPlane")
    assert control_plane["spec"]["replicas"] == 3
    assert control_plane["spec"]["version"] == "v1.31.2"
    machine_deployment = next(item for item in manifests if item["kind"] == "MachineDeployment")
    assert machine_deployment["spec"]["replicas"] == 4


def test_capi_renderer_uses_current_capi_and_capm3_v1beta2_contracts():
    manifests = render_capi_metal3_manifests({
        "name": "target-01",
        "namespace": "metal3",
        "infrastructure_provider": "metal3",
        "control_plane_endpoint": "192.0.2.20",
        "control_plane_count": 1,
        "image_url": "https://images.example.test/ubuntu.qcow2",
        "control_plane_data_template": {
            "meta_data": {"objectNames": [{"key": "hostname", "object": "machine"}]},
            "network_data": {
                "links": {"ethernets": [{"type": "phy", "id": "control"}]},
                "networks": {"ipv4DHCP": [{"id": "control", "link": "control"}]},
            },
        },
        "worker_pools": [{
            "name": "workers",
            "count": 2,
            "data_template": {
                "meta_data": {"objectNames": [{"key": "hostname", "object": "machine"}]},
                "network_data": {
                    "links": {"ethernets": [{"type": "phy", "id": "data"}]},
                    "networks": {"ipv4DHCP": [{"id": "data", "link": "data"}]},
                },
            },
        }],
    })

    api_versions = {item["kind"]: item["apiVersion"] for item in manifests}
    assert api_versions["Cluster"] == "cluster.x-k8s.io/v1beta2"
    assert api_versions["MachineDeployment"] == "cluster.x-k8s.io/v1beta2"
    assert api_versions["Metal3Cluster"] == "infrastructure.cluster.x-k8s.io/v1beta2"
    assert api_versions["Metal3MachineTemplate"] == "infrastructure.cluster.x-k8s.io/v1beta2"
    assert api_versions["KubeadmControlPlane"] == "controlplane.cluster.x-k8s.io/v1beta2"
    assert api_versions["KubeadmConfigTemplate"] == "bootstrap.cluster.x-k8s.io/v1beta2"

    cluster = next(item for item in manifests if item["kind"] == "Cluster")
    assert cluster["spec"]["controlPlaneRef"]["apiGroup"] == "controlplane.cluster.x-k8s.io"
    assert "apiVersion" not in cluster["spec"]["controlPlaneRef"]
    assert cluster["spec"]["infrastructureRef"]["apiGroup"] == "infrastructure.cluster.x-k8s.io"
    assert "apiVersion" not in cluster["spec"]["infrastructureRef"]


def test_capi_renderer_requires_explicit_image_url_and_supported_provider():
    base = {
        "name": "target-01",
        "control_plane_endpoint": "192.0.2.20",
        "worker_pools": [],
    }

    with pytest.raises(Metal3SkillError, match="CAPI_MACHINE_IMAGE_URL_REQUIRED"):
        render_capi_metal3_manifests(base)

    with pytest.raises(Metal3SkillError, match="CAPI_INFRASTRUCTURE_PROVIDER_UNSUPPORTED"):
        render_capi_metal3_manifests({
            **base,
            "infrastructure_provider": "openstack",
            "image_url": "https://images.example.test/image.qcow2",
        })


def test_capi_renderer_rejects_invalid_kubernetes_version_and_worker_pool_duplicates():
    base = {
        "name": "target-01",
        "control_plane_endpoint": "192.0.2.20",
        "image_url": "https://images.example.test/image.qcow2",
        "control_plane_data_template": {
            "meta_data": {"objectNames": [{"key": "hostname", "object": "machine"}]},
            "network_data": {
                "links": {"ethernets": [{"type": "phy", "id": "control"}]},
                "networks": {"ipv4DHCP": [{"id": "control", "link": "control"}]},
            },
        },
        "worker_pools": [],
    }

    with pytest.raises(Metal3SkillError, match="CAPI_KUBERNETES_VERSION_INVALID"):
        render_capi_metal3_manifests({**base, "k8s_version": "latest"})

    with pytest.raises(Metal3SkillError, match="CAPI_WORKER_POOL_DUPLICATE"):
        render_capi_metal3_manifests({
            **base,
            "worker_pools": [
                {
                    "name": "workers",
                    "count": 1,
                    "data_template": {
                        "meta_data": {"objectNames": [{"key": "hostname", "object": "machine"}]},
                        "network_data": {
                            "links": {"ethernets": [{"type": "phy", "id": "data"}]},
                            "networks": {"ipv4DHCP": [{"id": "data", "link": "data"}]},
                        },
                    },
                },
                {
                    "name": "workers",
                    "count": 2,
                    "data_template": {
                        "meta_data": {"objectNames": [{"key": "hostname", "object": "machine"}]},
                        "network_data": {
                            "links": {"ethernets": [{"type": "phy", "id": "data"}]},
                            "networks": {"ipv4DHCP": [{"id": "data", "link": "data"}]},
                        },
                    },
                },
            ],
        })
