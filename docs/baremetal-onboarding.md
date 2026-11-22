# Bare-metal onboarding walkthrough (operator view)

This is the full path a physical machine travels through the product, in the
order the UI enforces. Every step is an idempotent API call with a confirmation
string, so a half-run step can be repeated without duplicating work.

## 0. Prerequisites

- The EPH node is registered and online (`Infrastructure Agents`).
- The target network profile exists: provisioning and control networks with
  addresses, VLAN/bond layout and the default route (`Infrastructure Agents`).
- Credentials for the BMC exist in the secret store under a reference such as
  `vault/bmc/rack-1`. The platform stores the reference only.

## 1. Register the BMC

Fleet Inventory → the machine row → **BMC**. Fill the private-network Redfish
endpoint (`https://10.0.0.11`) and the credential reference. Nothing else can
run before this: PXE boot, discovery and firmware all read Redfish.

The row then shows the BMC host (never the credential) and the lifecycle state.

## 2. Boot the machines once over PXE

Fleet Inventory toolbar → **Plan PXE boot** to preview the per-machine plan, then
**Execute PXE boot** with the `PXE_ONCE` confirmation. This only sets a one-time
boot override; it never provisions anything.

## 3. Discover, then inspect

**Run discovery (PXE + inspect)** confirms both steps at once. The platform
boots the machines, waits for them to become reachable, and only for machines
that actually booted does it queue `metal3.host.inspect`. The EPH reports NICs,
addresses, SR-IOV PCI addresses and BMC identity; the fleet refresh applies the
report automatically — there is no separate "ingest" button to forget.

`No machine booted` is a semantic result, not a failure: check the BMC plan and
the provisioning network before retrying.

## 4. Review the hardware identity

Each row shows the identity verdict and any conflicts. When it is acceptable,
Fleet Inventory → **Lifecycle** → `identity_review`, with a reason. From
`identity_review` the machine can be `commissioned` (ready for delivery) or sent
back to `discovered`; a commissioned machine can later be `retired`, and a
retired machine rediscovered. The transition table lives in
`packages/agent_contracts/machine_lifecycle.py`, so the API and the UI cannot
disagree.

## 5. Build the Ironic configuration

Infrastructure Agents → the deployment card → **Ironic template preview**. The
platform renders interface names, addresses and VLAN sub-interfaces from the
network profile and the observed inventory. Two things are always typed by the
operator and travel through the template into the backend: the image/downloader
URLs and the version/credential names. The preview lists warnings when the
provisioning network, interface or address is missing — those same warnings are
persisted in `preflight_result.network_defaults.warnings` and shown on the
deployment.

The ports a profile may bind come from the machines, not from memory. Saving a
profile (`PUT .../infrastructure-networks/`) answers with `port_inventory`: every
enabled bond member and directly bound NIC is matched against the `nics` each
machine reported after its discovery boot, by name, PCI address (`pci@0000:3b:00.0`
and `0000:3b:00.0` are the same port) or MAC. A port no host reports is a warning
(`INFRA_NETWORK_PORT_NOT_COLLECTED`) naming the hosts that lack it, not an error —
the profile is usually written before every machine is discovered. Bonds, bridges
and VLAN sub-interfaces are skipped on purpose: the OS assembles them, so their
member ports are the hardware facts to check. SR-IOV PCI addresses are the same
deal: leave the list empty and the platform fills it from the collected devices
(`network_defaults.sriov_pci_sources` records where each address came from).

A `retired` machine is refused by PXE boot and discovery with
`409 MACHINE_NOT_BOOTABLE`: return it to `discovered` through a lifecycle review
before reusing it.

## 6. Preflight, then deploy

**Sync EPH preflight** stores the EPH's own readiness snapshot in
`preflight_result.eph_preflight`; **Sync BMH inventory** stores the last
inspection in `preflight_result.bmh_inventory_sync`. A deployment detail read
reconciles both, so the page never shows stale evidence. Deploy, watch the
command stream, and use postflight/cancel for recovery.

## 7. Lab acceptance

`GET /api/v1/metal3/lab-workflow/` reports the static contract of
`.github/workflows/metal3-lab-e2e.yml`: the `self-hosted`/`metal3-lab`/`physical`
labels on the acceptance job, the `needs` gate on `ready == 'true'`, the dispatch
inputs and the required secret names. The Fleet Inventory shows this as the lab
workflow tag; secret presence is reported as `not_observable` on purpose,
because the platform cannot see GitHub secrets.

## Bonded control path and the VLAN that boots

The reference lab reaches every machine over one bonded control path. `bond_control`
carries the untagged control address (management, BMC virtual media, the Live ISO
and the control plane), and VLAN 70 (`sdi_semc`) stacks on that same bond as the
provisioning network. Everything the firmware and the installer touch therefore
rides the control members; the data bond is a separate fabric for storage and
tenant traffic.

Two consequences are enforced in code, not in the profile editor:

* A profile may name the VLAN segment by its sub-interface (`bond_control.70`).
  `plan_infra_networks` stores the base link and derives `<base>.<vlan>` itself,
  so networkData, the PXE port resolver and the host link check all name the same
  interface, and the sub-interface is never emitted as a bare physical NIC.
* A role that joins only a VLAN (storage, SR-IOV) still gets the bond and its
  members rendered in its networkData. nmstate cannot bring up a VLAN over a link
  it was not told to create, and a worker that boots with a missing bond is a
  provisioning failure that only shows up after the install.

`pxe_boot_ports` walks one VLAN hop back to the physical members, because a bond
and a VLAN are assembled by the OS and firmware PXE can boot only from a member
NIC. `boot_mac_from_inventory` then picks the MAC of those members from the PXE
inventory, so `bootMACAddress` never has to be typed into the UI.

A data bond may also be described by the PCI addresses PXE reported, with no
interface list at all: `bond.interfaces` then derives as `pci@<address>` for each
declared address, so the operator never retypes kernel names that the inventory
already knows. A bond with neither ports nor PCI addresses is still refused, so
the template can never render a bond with no members.
