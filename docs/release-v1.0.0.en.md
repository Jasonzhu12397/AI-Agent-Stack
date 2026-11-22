# AI Agent Stack 1.0.0 — Release Notes (English)

This release turns the "AI Agent SaaS platform" and the "three-Agent physical bare-metal delivery chain" into **one deployable, versioned, traceable artifact**: one code base, one version number, one offline image list and one fail-closed release gate.

- Version: `1.0.0`, declared once in `pyproject.toml`, read by `packages/version.py::project_version()`; `/api/v1/`, the release manifest and `apps/web-studio/package.json` all agree.
- License: Apache-2.0 (see `LICENSE`).
- History: see `CHANGELOG.md`.
- Positioning: **a code-level 1.0**. The automated baseline is fully green; the physical-lab items are listed in §6 and are not marked verified until they actually run.

---

## 1. Support matrix

| Dimension | What this release declares |
| --- | --- |
| Python | 3.12 |
| Backend | Django 5.x + DRF + Channels/Daphne (HTTP/ASGI), PostgreSQL 16 |
| Frontend | Vue 3 + TypeScript + Vite + Element Plus + Pinia |
| Target Kubernetes | kubeadm-provisioned; upgrade and rollback decide on the full `major.minor.patch` triple |
| Metal3/Ironic components | See `deploy/offline/images.json` (BMO, CAPM3, IPAM, IRSO, Ironic, IPA downloader, CAPI v1.14, Calico, Rook/Ceph, SR-IOV operator, vbmc/sushy) |
| BMC vendors | HPE iLO, Dell iDRAC, Inspur, H3C, Lenovo XCC (Redfish `UpdateService`) |
| Deployment shape | Physical bare metal, **no libvirt/KVM prerequisite**; single and three control planes both supported |
| Network contract | One `bond_control` carries BMC/vbmc, PXE, IPA, Live-OS and the control plane; VLAN 70 stacks on `bond_control` as provisioning; data is a separate bond |
| License | Apache-2.0 |

---

## 2. Architecture

```
                    ┌──────────────────────────────────────────────┐
  browser / API ───▶│ Platform API (Django/DRF) + Web Studio (Vue) │
                    │ tenancy/RBAC · Agent/Workflow/Knowledge      │
                    │ audit chain · budgets · approvals · dispatch │
                    └───────────────┬──────────────────────────────┘
                                    │ HTTPS + mTLS (agent transport)
                    ┌───────────────▼──────────────┐
                    │      Cloud Control Agent     │  platform orchestration
                    └───────────────┬──────────────┘
                                    │
              ┌─────────────────────▼─────────────────────┐
              │                  EPH Agent                │  Live-OS / boot host
              │ Metal3·Ironic orchestration · readiness    │
              │ control-stack manifests · local registry   │
              │ PXE inventory write-back                   │
              └─────────────────────┬─────────────────────┘
                                    │ in-cluster commands (lease, at-most-once)
                    ┌───────────────▼──────────────┐
                    │        Target Agent          │  runs inside the target
                    │ upgrade / rollback / node net │
                    └──────────────────────────────┘
```

Network, exactly as the lab is cabled:

```
bond_control (active-backup: eno1/eno2)
  ├── untagged            → control              172.18.0.0/24
  └── bond_control.70     → provisioning (VLAN70) 172.18.37.0/24  ← PXE / IPA / Live-OS

bond_data (802.3ad: pci@0000:3b:00.0 / .1)
  ├── bond_data.3220      → data (tenant)         10.0.10.0/24
  └── bond_data.3200      → storage-internal      10.130.60.0/24
```

A bond and a VLAN are assembled by the OS, so they are **never** a `bootMACAddress`: firmware PXE can boot only from a member NIC. `pxe_boot_ports` walks one VLAN hop back to the member ports, and `boot_mac_from_inventory` takes the MAC of those ports from the PXE inventory.

---

## 3. What is in the release

### 3.1 SaaS platform

- Multi-tenancy: tenants, projects, `admin·editor·viewer`, server-side isolation.
- Agents: definitions, versions, publishing, run records, asynchronous LLM runs.
- Multi-Agent teams: sequential, coordinator, parallel fan-out, router.
- Workflows: `start·llm·agent·condition·knowledge_search·tool·approval·loop·end`; parallel any/all joins, single-approver resume, bounded checkpointed loops.
- Models: OpenAI-compatible gateway (chat + embeddings).
- Tools and integrations: HTTP / OpenAPI / MCP streamable-HTTP, review and permission grants.
- Knowledge: Qdrant collections versioned per embedding model and dimension.
- Async execution: PostgreSQL lease queue, heartbeats, retries, cancellation; mutating commands at-most-once.
- Usage governance: per-tenant token budget reservation, settlement, overspend protection; chained-hash audit with verification APIs.
- Control workspace: installation-scoped projects, versioned skills, plugin references, tasks/DAG, leased work.

### 3.2 Infrastructure delivery

- Lifecycle state machine: preflight → plan → approval → dispatch → reconcile → done/failed.
- EPH: Metal3/Ironic bare-metal orchestration, readiness, Live-OS boot, local registry, control-stack manifests.
- Target: Kubernetes upgrade, rollback, node network observation, CAPI worker provisioning.
- BMC firmware: Redfish `UpdateService` inventory, administrator-only `SimpleUpdate`, idempotent jobs, task-monitor polling, audit.
- PXE inventory write-back: one confirmed action boots the whole fleet through a Redfish one-time PXE override and collects inventory into `hardware_inventory`.

### 3.3 Metal3 delivery chain

- Network contract `infra_networks`: profile → three-role `networkData`, IPPools, SR-IOV NodePolicies, derived network spec.
- Rules live in the contract, not in the UI: VLAN sub-interface normalization, a VLAN-only role renders the bond it stacks on, PXE ports resolve through one VLAN hop, data-bond members derive from PCI addresses.
- Ironic template `ironic_template`: derives `interface`/`ip_address`/`dhcp` from the provisioning network; **image URLs are never defaulted** and are listed in `manual_fields` for the operator to fill in the UI.
- BMC network preflight `bmc_network`: BMC must sit on the control subnet; emulator ports are compared with the ports vbmc/sushy actually publish.
- Port inventory check `port_inventory_findings`: declared ports are compared with collected inventory per port, reporting `missing_on` and warnings.
- Acceptance and gate: `metal3_acceptance.py` (preflight/redfish/verify/rollback, read-only), `metal3_release_run.py` (all stages in one command), `metal3_release_evidence.py` (fail-closed manifest with hashes, commit and `product_version`).

### 3.4 Kubernetes upgrade and rollback

- Upgrade: rolling control plane (local apply + per-peer), etcd quorum guarded throughout, `ssh.reachable` and `ssh.sudo` preflight, full version-triple comparison.
- Rollback: per-member restore on a stacked etcd, peer URL ↔ node matching (IPv6 included), snapshot restricted to `/var/lib/etcd`, remote staging `scp → /tmp → sudo install -m 0444`.
- Failure recovery: one `_fail` path restores swapped directories, starts every stopped kubelet with the local member first, probes the apiserver, and returns `recovery` plus a three-branch `next_step`.

### 3.5 Frontend (Web Studio)

Infrastructure view (network profile editing, inventory-driven port/PCI selects, port gaps, BMC fabric, EPH preflight verdict and warnings, deployment detail reading `preflight_result`), Metal3 fleet view (assets/BMC/firmware/lab workflow shape and last run, including physical runner label checks), governance, audit, integration hub, agents and teams, workflow DAG.

### 3.6 Offline assets

`deploy/offline/images.json` (offline image list), `deploy/offline/addons/control-stack` (vendor and kustomize variable contract, guarded by tests), `deploy/docker-compose.yml`, `deploy/docker-compose.cloud.yml`, `eph-iso/` (Live-OS build entry).

---

## 4. Deployment

### 4.1 Development / single host

```bash
cp .env.example .env
make dev        # docker compose up --build
make verify     # ruff + pytest + frontend tests + frontend build
make version    # 1.0.0
```

### 4.2 Production (cloud control plane)

`deploy/docker-compose.cloud.yml`; secrets come from `deploy/vault/` and `.env`. Production must set a non-default `SECRET_KEY`, disable `DEBUG`, and serve `/health/live` and `/health/ready`.

### 4.3 Offline metal room

1. On a connected machine, pull the images in `deploy/offline/images.json` and push them to the intranet registry (change `registry_endpoint`).
2. Import the images on site and start the compose stack or the cloud control plane.
3. Configure the network in the UI: `bond_control` carries control and VLAN 70 provisioning; the data bond stacks storage/tenant VLANs; image URLs are typed by the operator.
4. Trigger the fleet PXE collection and confirm `hardware_inventory` is written back with no port/PCI/BMC warnings in preflight.
5. Submit the deployment plan → preflight → approval → dispatch.

### 4.4 Lab acceptance (physical)

```bash
tools/metal3_acceptance.py preflight --out evidence/preflight.json
tools/metal3_acceptance.py redfish   --out evidence/redfish.json
tools/metal3_acceptance.py verify    --out evidence/acceptance.json
tools/metal3_acceptance.py rollback --snapshot /var/lib/etcd/backups/<pre>.db \
    --out evidence/rollback.json
tools/metal3_release_run.py     # all stages + sealed release manifest
```

Or dispatch `.github/workflows/metal3-lab-e2e.yml` from the Web Studio Metal3 fleet page (runner labels `self-hosted, metal3-lab, physical`).

---

## 5. Automated baseline

| Item | Result |
| --- | --- |
| Full pytest | 1666 passed / 15 skipped |
| Ruff, whole repository | All checks passed |
| Frontend `npm test` | 36 pass / 0 fail |
| Frontend `npm run build` | succeeded (existing chunk-size warning only) |
| CI | Compose full-stack smoke, kind + Metal3 CRD integration, PostgreSQL/mTLS integration, four pytest shards, all image builds: passed |
| Version consistency | `/api/v1/`, `pyproject.toml`, `package.json`, release manifest all `1.0.0`, asserted dynamically by `tests/test_health.py` |

Evidence points at a specific run and commit, recorded in `docs/verification-matrix.md`.

---

## 6. GA gate: still requires real hardware (not claimed verified)

1. Real Redfish BMC responses and firmware `SimpleUpdate` (one each of HPE / Dell / Inspur / H3C / Lenovo).
2. Real firmware PXE boot: whether a `bootMACAddress` taken from a bond member NIC reaches IPA.
3. Physical `bond_control` + VLAN 70 assembly, and correct Live-OS / IPA addressing.
4. `snapshot status` and per-member restore on a real etcd static pod.
5. The `ssh` key-based + `NOPASSWD sudo` channel for multi-control-plane rollback.
6. Rook-Ceph and SR-IOV on real NICs.
7. A `release-manifest.json` with `status: pass` produced by `metal3_release_run.py` — the only thing that turns 1.0.0 into "physically verified".

Until all seven pass, this document and §5 must not be rewritten as verified. Each item is ticked with the run and commit that proved it.

---

## 7. Known limitations (deliberate boundaries)

- Package downgrades are out of scope: kubeadm has no supported downgrade path, so rollback restores control-plane state only.
- Switch VLANs and ports are not validated (the site owns them).
- WebSocket consumers are not implemented; realtime capability is HTTP/polling.
- OIDC/OAuth2, invitation email, password reset, service identities and fine-grained resource/tool policies are not implemented.
- Real Qdrant paths and budget behaviour under concurrency still need real verification.
- Ironic image URLs, credential name and version are manual fields and are never auto-filled.

---

## 8. Release artifacts

- Git tag: `v1.0.0`
- GitHub Release: the full text of this note (Chinese and English)
- Source archives and checksums: GitHub-generated `tarball` / `zipball`
- License: `LICENSE` (Apache-2.0)
- History: `CHANGELOG.md`
