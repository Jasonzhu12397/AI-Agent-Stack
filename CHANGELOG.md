# Changelog

All notable changes to AI Agent Stack are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-11-22

The first release that is deployable as a product: the SaaS Agent platform and
the three-Agent bare-metal delivery chain in one versioned artifact, with one
automated baseline, one offline image list and one release gate.

### Added

- Single source for the product version (`packages/version.py`), read from
  `pyproject.toml`, exposed on `/api/v1/`, stamped into the release manifest and
  overridable with `AI_AGENT_STACK_VERSION` for stripped images.
- `LICENSE` (Apache-2.0).
- `docs/release-v1.0.0.zh-CN.md` and `docs/release-v1.0.0.en.md`: feature
  inventory, deploy paths, support matrix, GA gate list and the items that still
  require a physical lab, in Chinese and English.
- `make verify` (ruff + pytest + frontend tests + frontend build) and
  `make version`.
- `tests/test_dockerfile_packages.py`: every Python image must copy the whole
  `packages/` tree, so a new shared module can never break an image again.
- Release manifest now records `product_version` beside `commit` and evidence
  hashes, so two releases cannot be confused by an identical evidence directory.

### Changed

- Every Python image copies `packages/` as a tree instead of a hand-listed
  subset of subdirectories; `.dockerignore` excludes caches and build output.
- `tests/test_health.py` asserts the version from `project_version()` instead of
  a literal, and `metal3_release_evidence` takes the same value.
- Version bumped to `1.0.0` in `pyproject.toml`, `apps/web-studio/package.json`
  and the lockfile.

### Fixed (root causes, from the v0.3.0 cycle)

- **Rollback can no longer leave a cluster down**: one `_fail` path restores
  swapped data directories, starts every stopped kubelet with the local member
  first, probes the apiserver, and returns `recovery` plus a three-branch
  `next_step`.
- **Upgrade completion compares the full version triple**: `v1.34.0` is no
  longer treated as satisfying `v1.34.3`.
- **VLAN topology renders correctly**: a profile may declare a segment by its
  sub-interface (`bond_control.70`), the contract stores the base and derives
  `<base>.<vlan>`; a role that joins only a VLAN gets the bond and its members
  rendered, and `pxe_boot_ports` walks one VLAN hop back to physical members.
- **A data bond's members can come from PXE-collected PCI addresses**
  (`pci@<addr>`), so the UI never retypes kernel names; an empty bond is still
  refused.
- **Snapshot visibility, remote sudo and IPv6 peers**: snapshot must live under
  `/var/lib/etcd`, `ssh.sudo/<node>` is a preflight check, and bracketed IPv6
  peer URLs match their members.
- **Acceptance evidence names what would change**: `local_node`, `members` and
  the five rollback stages, so an approver reviews mutations, not a summary.

## [0.3.0] — 2026-11-22

### Added

- Version read from one place, stamped into the release manifest.
- Release notes for the bare-metal delivery baseline.
- `make verify` and `make version`.

### Fixed

- Images that copied a hand-listed subset of `packages/` failed to boot with
  `ModuleNotFoundError` when a new shared module appeared.

## [0.2.0] — earlier

Django/Vue migration baseline: multi-tenant projects and roles, Agent and
Workflow APIs, durable PostgreSQL worker, Metal3 cluster-plan validation,
EPH pre-flight orchestration, control workspace and the three infrastructure
Agents. Test baseline at that time: 103 passed.

## Earlier history

Before `0.2.0` the repository ran a FastAPI + React stack; that history stays in
the git log and in `docs/verification-matrix.md`, and is not re-versioned here.
