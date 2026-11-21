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
