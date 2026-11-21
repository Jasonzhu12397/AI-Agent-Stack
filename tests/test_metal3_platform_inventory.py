"""The BMH bundle is generated data, not a hand-maintained inventory file.

The platform already stores each machine's BMC endpoint, the vault reference for
its credentials and the NICs the agent reported. These tests pin the mapping
from ``AgentMachine`` rows to BareMetalHost manifests, including the refusal to
guess a boot NIC.
"""

from __future__ import annotations

import pytest
from core.models import (
    AgentMachine,
    AgentNode,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from django.test import override_settings

from tools.bmh_from_inventory import InventoryError, render_documents
from tools.bmh_from_platform import (
    boot_mac_from_inventory,
    load_platform_machines,
    slugify_bmh_name,
)

RESOLVER = "tests.test_metal3_platform_inventory.stub_resolver"
SCHEME = "redfish+https://10.0.0.11/redfish/v1/Systems/1"


def stub_resolver(**kwargs):
    return {"username": "admin", "password": "vault-secret"}


@pytest.fixture
def platform_fleet(db):
    user = get_user_model().objects.create_user(username="fleet-owner", password="test-password")
    tenant = Tenant.objects.create(name="Fleet tenant", slug="fleet-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.OWNER)
    project = Project.objects.create(tenant=tenant, name="Fleet project", slug="fleet-project")
    ProjectMembership.objects.create(project=project, user=user, role=ProjectMembership.Role.ADMIN)

    def add_node(
        name, inventory, *, endpoint="https://10.0.0.11", ref="vault/kv/bmc/team", tls=True
    ):
        node = AgentNode.objects.create(
            tenant=tenant,
            project=project,
            created_by=user,
            name=name,
            node_type=AgentNode.NodeType.EPH_NODE,
            credential_hash="c" * 64,
        )
        return AgentMachine.objects.create(
            tenant=tenant,
            project=project,
            agent_node=node,
            hardware_inventory=inventory,
            bmc_endpoint=endpoint,
            bmc_credential_ref=ref,
            bmc_tls_verify=tls,
        )

    return tenant, project, add_node


def test_platform_rows_become_baremetalhosts(platform_fleet):
    tenant, project, add_node = platform_fleet
    add_node("bm-node-1", {"status": "observed", "nics": [{"mac": "AA-BB-CC-DD-EE-01"}]})

    with override_settings(BMC_CREDENTIAL_RESOLVER=RESOLVER):
        machines = load_platform_machines(project_id=str(project.id))

    assert [item.name for item in machines] == ["bm-node-1"]
    machine = machines[0]
    assert machine.bmc_address == SCHEME
    assert machine.boot_mac == "aa:bb:cc:dd:ee:01"
    assert machine.username == "admin"
    assert machine.password == "vault-secret"
    assert machine.verify_bmc_ca is True

    documents = render_documents(machines)
    assert [document["kind"] for document in documents] == ["Secret", "BareMetalHost"]
    host = documents[1]
    assert host["spec"]["bmc"]["address"] == SCHEME
    assert host["spec"]["bootMACAddress"] == "aa:bb:cc:dd:ee:01"
    assert host["spec"]["bmc"]["disableCertificateVerification"] is False
    assert host["metadata"]["labels"]["ai-agent-stack.io/machine-id"]


def test_named_boot_nic_wins_over_the_others(platform_fleet):
    tenant, project, add_node = platform_fleet
    add_node(
        "bm-node-2",
        {
            "status": "observed",
            "nics": [
                {"mac": "aa:bb:cc:dd:ee:02", "name": "eth0"},
                {"mac": "aa:bb:cc:dd:ee:03", "name": "eth1", "boot": True},
            ],
        },
    )

    with override_settings(BMC_CREDENTIAL_RESOLVER=RESOLVER):
        machines = load_platform_machines(project_id=str(project.id))

    assert machines[0].boot_mac == "aa:bb:cc:dd:ee:03"


def test_ambiguous_nics_are_skipped_not_guessed(platform_fleet):
    tenant, project, add_node = platform_fleet
    add_node(
        "bm-node-3",
        {
            "status": "observed",
            "nics": [
                {"mac": "aa:bb:cc:dd:ee:04", "name": "eth0"},
                {"mac": "aa:bb:cc:dd:ee:05", "name": "eth1"},
            ],
        },
    )
    skipped: list[str] = []

    with override_settings(BMC_CREDENTIAL_RESOLVER=RESOLVER):
        machines = load_platform_machines(project_id=str(project.id), skipped=skipped)

    assert machines == []
    assert skipped and "no unambiguous boot NIC" in skipped[0]


def test_machine_without_bmc_configuration_is_reported(platform_fleet):
    tenant, project, add_node = platform_fleet
    add_node("bm-node-4", {"status": "observed", "boot_mac": "aa:bb:cc:dd:ee:06"}, endpoint="")
    skipped: list[str] = []

    with override_settings(BMC_CREDENTIAL_RESOLVER=RESOLVER):
        machines = load_platform_machines(project_id=str(project.id), skipped=skipped)

    assert machines == []
    assert any("bmc_endpoint is not configured" in item for item in skipped)


def test_missing_resolver_never_invents_credentials(platform_fleet):
    tenant, project, add_node = platform_fleet
    add_node("bm-node-5", {"status": "observed", "boot_mac": "aa:bb:cc:dd:ee:07"})
    skipped: list[str] = []

    with override_settings(BMC_CREDENTIAL_RESOLVER="", BMC_VAULT_ADDR=""):
        machines = load_platform_machines(project_id=str(project.id), skipped=skipped)

    assert machines == []
    assert any("BMC_CREDENTIAL_RESOLVER is not configured" in item for item in skipped)


def test_tls_verify_drives_certificate_checking(platform_fleet):
    tenant, project, add_node = platform_fleet
    add_node("bm-node-6", {"status": "observed", "boot_mac": "aa:bb:cc:dd:ee:08"}, tls=False)

    with override_settings(BMC_CREDENTIAL_RESOLVER=RESOLVER):
        machines = load_platform_machines(project_id=str(project.id))

    host = render_documents(machines)[1]
    assert host["spec"]["bmc"]["disableCertificateVerification"] is True


def test_duplicate_boot_macs_across_machines_are_rejected(platform_fleet):
    tenant, project, add_node = platform_fleet
    add_node("bm-node-7", {"status": "observed", "boot_mac": "aa:bb:cc:dd:ee:09"})
    add_node(
        "bm-node-8",
        {"status": "observed", "boot_mac": "aa:bb:cc:dd:ee:09"},
        endpoint="https://10.0.0.12",
    )

    with override_settings(BMC_CREDENTIAL_RESOLVER=RESOLVER):
        with pytest.raises(InventoryError, match="duplicate boot MAC"):
            load_platform_machines(project_id=str(project.id))


def test_boot_mac_reads_explicit_field_first():
    assert boot_mac_from_inventory({"boot_mac": "AA:BB:CC:DD:EE:0A"}) == "aa:bb:cc:dd:ee:0a"
    with pytest.raises(InventoryError):
        boot_mac_from_inventory({"boot_mac": "not-a-mac"})


def test_slugify_keeps_the_node_identity():
    assert slugify_bmh_name("BM_Node 1") == "bm-node-1"
    assert slugify_bmh_name("", fallback="node-2") == "node-2"
    assert len(slugify_bmh_name("a" * 60)) <= 40

def test_pxe_ports_resolve_a_multi_nic_boot_nic_by_pci_address():
    inventory = {
        "nics": [
            {"name": "eno1", "pciAddress": "0000:19:00.0", "mac": "AA:BB:CC:DD:EE:10"},
            {"name": "eno2", "pciAddress": "0000:19:00.1", "mac": "AA:BB:CC:DD:EE:11"},
            {"name": "eno3", "pciAddress": "0000:3b:00.0", "mac": "AA:BB:CC:DD:EE:12"},
            {"name": "eno4", "pciAddress": "0000:3b:00.1", "mac": "AA:BB:CC:DD:EE:13"},
        ]
    }
    # Two ports carry the PXE link, so two MACs are candidates and the answer
    # stays ambiguous instead of silently picking one.
    ports = ("pci@0000:3b:00.0", "pci@0000:3b:00.1")
    assert boot_mac_from_inventory(inventory, preferred_ports=ports) is None


def test_a_boot_flag_outside_the_pxe_link_is_refused():
    inventory = {
        "nics": [
            {"name": "eno1", "pxe": True, "mac": "aa:bb:cc:dd:ee:20"},
            {"name": "eno3", "pciAddress": "0000:3b:00.0", "mac": "aa:bb:cc:dd:ee:22"},
        ]
    }
    with pytest.raises(ValueError, match="not on the PXE link"):
        boot_mac_from_inventory(
            inventory, preferred_ports=("pci@0000:3b:00.0",)
        )


def test_pxe_port_matches_the_per_port_mac_even_for_a_multi_port_nic():
    inventory = {
        "nics": [
            {"name": "eth0", "mac": "aa:bb:cc:dd:ee:30"},
            {"name": "pci@0000:3b:00.0", "mac": "aa:bb:cc:dd:ee:31"},
        ]
    }
    assert (
        boot_mac_from_inventory(inventory, preferred_ports=("pci@0000:3b:00.0",))
        == "aa:bb:cc:dd:ee:31"
    )
