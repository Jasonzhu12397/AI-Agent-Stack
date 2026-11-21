"""The Ironic template the provisioning network implies is rendered once.

The UI used to compute interface/address itself in TypeScript while the
platform validator computed the same values in Python. These tests pin the
single renderer, the operator-owned fields it never invents, and the preview
endpoint that exposes it.
"""

import pytest
from core.models import Project, ProjectMembership, Tenant, TenantMembership
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from packages.agent_contracts.ironic_template import (
    MANUAL_FIELDS,
    WARNING_ADDRESS_UNAVAILABLE,
    WARNING_PROVISIONING_NETWORK_MISSING,
    ironic_template_for_profile,
)

BASE_NETWORKS = [
    {
        "name": "control",
        "role": "control",
        "vlan": 100,
        "mtu": 1500,
        "cidr": "10.0.0.0/24",
        "gateway_ipv4": "10.0.0.1",
        "ip_pools": [{"start": "10.0.0.10", "end": "10.0.0.50"}],
        "interface": "eno1",
        "host_roles": ["control_plane", "worker"],
    },
    {
        "name": "data",
        "role": "data",
        "vlan": 200,
        "mtu": 9000,
        "cidr": "10.10.0.0/24",
        "gateway_ipv4": "10.10.0.1",
        "ip_pools": [{"start": "10.10.0.10", "end": "10.10.0.50"}],
        "interface": "eno2",
        "host_roles": ["worker"],
    },
    {
        "name": "storage",
        "role": "storage-internal",
        "vlan": 300,
        "mtu": 9000,
        "cidr": "10.20.0.0/24",
        "gateway_ipv4": "10.20.0.1",
        "ip_pools": [{"start": "10.20.0.10", "end": "10.20.0.50"}],
        "interface": "eno3",
        "host_roles": ["storage"],
    },
]

PROVISIONING = {
    "name": "provisioning",
    "role": "provisioning",
    "vlan": 400,
    "mtu": 1500,
    "cidr": "10.40.0.0/24",
    "gateway_ipv4": "10.40.0.1",
    "ip_pools": [{"start": "10.40.0.10", "end": "10.40.0.50"}],
    "interface": "eno4",
    "host_roles": ["worker"],
}


def _profile(provisioning=PROVISIONING):
    return {"networks": [*BASE_NETWORKS, provisioning]}


def test_template_names_the_provisioning_sub_interface():
    template = ironic_template_for_profile(_profile())

    assert template["provisioning_network"] == "provisioning"
    # The interface comes from the plan; the address is derived from the
    # provisioning subnet and never from the gateway of that subnet.
    assert template["configuration"]["networking"] == {
        "interface": "eno4.400",
        "ip_address": "10.40.0.2/24",
    }
    assert template["defaults_sources"] == ["interface", "ip_address_subnet"]


def test_image_urls_and_identity_stay_operator_owned():
    template = ironic_template_for_profile(_profile())

    images = template["configuration"]["images"]
    assert images == {
        "ironic": "",
        "deploy_ramdisk_downloader": "",
        "keepalived": "",
    }
    assert template["configuration"]["api_credentials_name"] == ""
    assert template["configuration"]["version"] == ""
    assert "images.ironic" in MANUAL_FIELDS
    assert all(field in MANUAL_FIELDS for field in ("api_credentials_name", "version"))
    assert "images.ironic" not in template["defaults_sources"]


def test_bond_is_the_link_when_there_is_no_vlan():
    bond = {
        "name": "provisioning",
        "role": "provisioning",
        "mtu": 1500,
        "cidr": "10.40.0.0/24",
        "gateway_ipv4": "10.40.0.1",
        "ip_pools": [{"start": "10.40.0.10", "end": "10.40.0.50"}],
        "bond": {
            "name": "bond-prov",
            "interfaces": ["eno4", "eno5"],
            "mode": "active-backup",
        },
        "host_roles": ["worker"],
    }
    template = ironic_template_for_profile(_profile(bond))

    assert template["configuration"]["networking"]["interface"] == "bond-prov"


def test_eph_owned_address_beats_the_gateway_default():
    template = ironic_template_for_profile(_profile(), eph_address="10.40.0.30/24")

    assert template["configuration"]["networking"]["ip_address"] == "10.40.0.30"


def test_profile_without_provisioning_network_reports_the_gap():
    template = ironic_template_for_profile({"networks": BASE_NETWORKS})

    assert template["provisioning_network"] is None
    assert template["configuration"]["networking"] == {}
    assert WARNING_PROVISIONING_NETWORK_MISSING in template["warnings"]


def test_provisioning_network_without_address_reports_the_gap():
    # Every usable host of the subnet is already spoken for, so nothing may be
    # handed to ironic and the operator has to supply the address.
    provisioning = {
        **PROVISIONING,
        "cidr": "10.40.0.0/30",
        "gateway_ipv4": "10.40.0.1",
        "ip_pools": [{"start": "10.40.0.2", "end": "10.40.0.2"}],
    }
    template = ironic_template_for_profile(_profile(provisioning))

    assert template["configuration"]["networking"] == {"interface": "eno4.400"}
    assert WARNING_ADDRESS_UNAVAILABLE in template["warnings"]


@pytest.fixture
def template_workspace(db):
    user = get_user_model().objects.create_user(
        username="template-editor",
        password="test-password",
    )
    tenant = Tenant.objects.create(name="Template tenant", slug="template-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="member")
    project = Project.objects.create(
        tenant=tenant,
        name="Template project",
        slug="template-project",
    )
    ProjectMembership.objects.create(project=project, user=user, role="editor")
    return user, tenant, project


@pytest.mark.django_db
def test_preview_endpoint_returns_the_same_template(template_workspace):
    user, tenant, project = template_workspace
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/cluster-deployments/ironic-template/",
        {
            "infra_network_profile": _profile(),
            "eph_network_addresses": {"provisioning": "10.40.0.31/24"},
        },
        format="json",
    )

    assert response.status_code == 200
    assert response.data == ironic_template_for_profile(
        _profile(),
        eph_address="10.40.0.31/24",
    )


@pytest.mark.django_db
def test_preview_endpoint_rejects_an_invalid_profile(template_workspace):
    user, tenant, project = template_workspace
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/cluster-deployments/ironic-template/",
        {"infra_network_profile": {"networks": []}},
        format="json",
    )

    assert response.status_code == 400
    assert response.data["code"] == "ironic_template_profile_invalid"


def test_the_gateway_is_never_used_as_the_provisioning_address():
    template = ironic_template_for_profile(_profile())

    networking = template["configuration"]["networking"]
    assert networking["ip_address"] != PROVISIONING["gateway_ipv4"]
    assert networking["ip_address"] == "10.40.0.2/24"
    assert "ip_address_subnet" in template["defaults_sources"]


def test_a_dhcp_provisioning_network_needs_no_static_address():
    provisioning = {**PROVISIONING, "dhcp": True, "gateway_ipv4": None}
    template = ironic_template_for_profile(_profile(provisioning))

    assert template["configuration"]["networking"] == {
        "interface": "eno4.400",
        "dhcp": True,
    }
    assert WARNING_ADDRESS_UNAVAILABLE not in template["warnings"]
