"""Merge inspected BareMetalHost records into machine inventory.

Both the manual ingest endpoint and the control-plane sync endpoint must apply
exactly the same rules, so the merge lives here instead of inside a view.
"""

from __future__ import annotations

from typing import Any

from core.models import AgentMachine


def merge_bmh_host(machine: AgentMachine, host: dict[str, Any], now) -> bool:
    """Merge one inspected BareMetalHost into a machine record.

    The BMH block is always recorded as discovery evidence. NIC records are
    adopted only when they carry PCI addresses and the machine has no
    PCI-bearing NIC inventory of its own, so an agent observation is never
    replaced by the weaker BMH view. Returns whether PCI inventory was adopted.
    """

    inventory = dict(machine.hardware_inventory or {})
    inventory["bmh"] = {**host, "inspected_at": now.isoformat()}
    fields: list[str] = ["hardware_inventory"]
    adopted_pci = False
    existing_nics = inventory.get("nics")
    has_pci = isinstance(existing_nics, list) and any(
        isinstance(nic, dict) and nic.get("pci_address") for nic in existing_nics
    )
    pci_addresses = host.get("pci_addresses") if isinstance(host.get("pci_addresses"), list) else []
    # The BMH view is the weaker source, but while it *is* the only source a
    # later inspection is the newer truth: refusing it froze a partial NIC
    # inventory forever. Only an agent-owned inventory outranks the BMH.
    bmh_owned = inventory.get("nics_source") == "bmh_inspect"
    adopt = bool(pci_addresses) and (not has_pci or bmh_owned)
    nics = [
        {
            "name": nic["name"],
            "pci_address": nic["pci_address"],
            "mac": nic.get("mac", ""),
            "sriov_capable": bool(nic["pci_address"]),
        }
        for nic in host.get("nics") or []
        if isinstance(nic, dict) and nic.get("name") and nic.get("pci_address")
    ]
    if adopt and nics != existing_nics:
        inventory["nics"] = nics
        inventory["nics_source"] = "bmh_inspect"
        adopted_pci = True
        machine.inventory_source = "bmh_inspect"
        machine.inventory_revision = (machine.inventory_revision or 0) + 1
        machine.observed_at = now
        fields.extend(["inventory_source", "inventory_revision", "observed_at"])
    if not machine.bmh_name:
        machine.bmh_name = host["bmh_name"]
        fields.append("bmh_name")
    if not machine.system_uuid and host.get("system_uuid"):
        machine.system_uuid = host["system_uuid"]
        fields.append("system_uuid")
    if not machine.vendor and host.get("manufacturer"):
        machine.vendor = host["manufacturer"]
        fields.append("vendor")
    if not machine.product_name and host.get("product_name"):
        machine.product_name = host["product_name"]
        fields.append("product_name")
    machine.hardware_inventory = inventory
    machine.save(update_fields=sorted(set(fields)) + ["updated_at"])
    return adopted_pci


def match_machines(machines, hosts):
    """Match hosts to machines by BMC host, then BMH name, then system UUID."""

    from packages.agent_contracts.metal3_bmh_inventory import bmc_host_key

    by_bmc = {
        key: machine for machine in machines if (key := bmc_host_key(machine.bmc_endpoint))
    }
    by_bmh = {machine.bmh_name: machine for machine in machines if machine.bmh_name}
    by_uuid = {machine.system_uuid: machine for machine in machines if machine.system_uuid}
    pairs: list[tuple[dict[str, Any], AgentMachine]] = []
    unmatched: list[str] = []
    for host in hosts:
        machine = None
        if host.get("bmc_host"):
            machine = by_bmc.get(host["bmc_host"])
        if machine is None:
            machine = by_bmh.get(host.get("bmh_name", ""))
        if machine is None and host.get("system_uuid"):
            machine = by_uuid.get(host["system_uuid"])
        if machine is None:
            unmatched.append(host.get("bmh_name", ""))
        else:
            pairs.append((host, machine))
    return pairs, unmatched
