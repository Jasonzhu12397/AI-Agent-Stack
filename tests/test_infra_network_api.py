from __future__ import annotations

import pytest
from core.models import (
    InfrastructureNetworkProfile,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def network_workspace(db):
    user = get_user_model().objects.create_user(
        username="network-admin",
        password="test-password",
    )
    tenant = Tenant.objects.create(name="Network tenant", slug="network-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(
        tenant=tenant, name="Network project", slug="network-project"
    )
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return user, tenant, project, client


def _profile() -> dict:
    return {
        "dns_servers": ["10.0.60.53"],
        "networks": [
            {
                "name": "ccd-control",
                "role": "control",
                "vlan": 3220,
                "cidr": "10.0.60.0/24",
                "gateway_ipv4": "10.0.60.1",
                "ip_pools": [
                    {"start": "10.0.60.10", "end": "10.0.60.250", "prefix": 24}
                ],
                "bond": {
                    "name": "bond_control",
                    "interfaces": ["eno1", "eno2"],
                    "mode": "active-backup",
                },
                "host_roles": ["control_plane", "worker", "storage"],
            },
            {
                "name": "ccd-data",
                "role": "data",
                "vlan": 3300,
                "cidr": "10.0.61.0/24",
                "gateway_ipv4": "10.0.61.1",
                "ip_pools": [
                    {"start": "10.0.61.10", "end": "10.0.61.250", "prefix": 24}
                ],
                "interface": "bond_data",
                "host_roles": ["worker", "storage"],
            },
            {
                "name": "ceph-cluster-net",
                "role": "storage-internal",
                "vlan": 3608,
                "cidr": "10.0.70.0/26",
                "gateway_ipv4": "10.0.70.1",
                "ip_pools": [
                    {"start": "10.0.70.2", "end": "10.0.70.62", "prefix": 26}
                ],
                "interface": "bond_storage",
                "host_roles": ["storage", "worker"],
            },
            {
                "name": "ceph-public-net",
                "role": "storage-external",
                "vlan": 3609,
                "cidr": "10.0.69.0/24",
                "gateway_ipv4": "10.0.69.1",
                "ip_pools": [
                    {"start": "10.0.69.71", "end": "10.0.69.232", "prefix": 24}
                ],
                "interface": "bond_storage",
                "host_roles": ["storage", "worker"],
            },
        ],
    }


@pytest.mark.django_db
def test_put_and_get_infrastructure_network_profile(network_workspace):
    _, tenant, project, client = network_workspace
    url = (
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/infrastructure-networks/"
    )
    response = client.put(url, {"profile": _profile()}, format="json")
    assert response.status_code == 200
    assert response.data["generated"]["role_network_data"]["control_plane"][
        "links"
    ]["bonds"][0]["id"] == "bond_control"
    assert InfrastructureNetworkProfile.objects.filter(
        tenant=tenant, project=project
    ).exists()
    fetched = client.get(url)
    assert fetched.status_code == 200
    assert len(fetched.data["profile"]["networks"]) == 4


@pytest.mark.django_db
def test_put_rejects_overlapping_cidrs(network_workspace):
    _, tenant, project, client = network_workspace
    profile = _profile()
    profile["networks"][1]["cidr"] = "10.0.60.0/25"
    profile["networks"][1]["gateway_ipv4"] = "10.0.60.1"
    profile["networks"][1]["ip_pools"] = [
        {"start": "10.0.60.10", "end": "10.0.60.30", "prefix": 25}
    ]
    url = (
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/infrastructure-networks/"
    )
    response = client.put(url, {"profile": profile}, format="json")
    assert response.status_code == 400
    assert response.data["detail"] == "INFRA_NETWORK_CIDR_OVERLAP"
