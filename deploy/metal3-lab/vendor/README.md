# Vendored Metal3 dev-env (physical machines only)

This directory is **repository code**. The upstream `metal3-io/metal3-dev-env`
tree at commit `7076f07624cdfba990f83bea24f749b37e3baad7` was copied into
`deploy/metal3-lab/vendor/metal3-dev-env/` so that:

- the lab runner never clones, fetches or checks out a git remote at run time;
- the scripts, libs and Ansible playbooks can be edited, reviewed and extended
  in-tree like any other file of this repository;
- behaviour fixes do not wait for an upstream release: change the code here.

## What is inside

Only the code this lab actually runs. `tools/vendor_import.py` imports the
upstream archive, writes provenance, then deletes every path in its
`PRUNE_PATHS` list. The retained tree is:

| Path | Role |
|---|---|
| `01_prepare_host.sh` | host package install, Ansible bootstrap, kind/kubectl/krew |
| `02_configure_host.sh` | provisioning/external bridges on real NICs, firewall, registry, ironic httpd |
| `03_launch_mgmt_cluster.sh` | kind management cluster, ironic/BMO/CAPI deploy, hands the plan to the agent |
| `04_verify.sh` | one call to `tools/metal3_deploy.py verify`; no cluster judgement in shell |
| `lib/*.sh` | shared helpers (common, network, download, releases, TLS, auth, images) |
| `Makefile`, `ansible.cfg`, `config_example.sh` | entry points and defaults |
| `vm-setup/inventory.ini`, `install-package-playbook.yml`, `requirements.yml`, `roles/packages_installation`, `roles/firewall` | the only Ansible that still runs (host packages and firewall) |
| `ubuntu_bridge_network_configuration.sh` | `ip`/`brctl` bridge setup used by `02` |
| `openstackclient.sh`, `clouds.yaml.j2` | ironic CLI wrapper rendered by `03` |
| `cluster_cleanup.sh`, `host_cleanup.sh` | `make clean` |
| `VENDOR.json` | provenance: upstream repo, commit, import source, file counts, pruned paths |
| `MANIFEST.sha256` | per-file digest list used for integrity checks |

`VENDOR.json` and `MANIFEST.sha256` are generated. The rest is a **fork**, not a
mirror: the physical-machine path is kept and rewritten to call python, and
every divergence is recorded in `PATCHES.md`. The tree is 38 files (upstream
ships 245); nothing that only served virtual machines survives.

## What is deliberately not vendored

The upstream tree ships a whole libvirt/KVM laboratory, its BMC emulators, a
VM-based e2e suite and developer tooling. None of it has a consumer here, and
dead code that nobody executes is dead code nobody should patch, so it is
removed on every import:

- `vm-setup/setup-playbook.yml`, `teardown-playbook.yml`, `vm-setup/library`
- `vm-setup/roles/libvirt`, `roles/virtbmc`, `roles/ovs`, `roles/common`
- `vbmc.sh`, `disable_apparmor_driver_libvirtd.sh`
- `PoCs/` (Kamaji demo: libvirt domains, sushy-tools, shell-generated BMHs)
- `tests/`, `template/` (VM-based e2e suite and its SARIF template)
- `tilt-setup/`, `Tiltfile`, `Tiltfile.md`, `hack/`, `ip_range_check.py`
- upstream repository metadata: `README.md`, `AGENTS.md`, `CLAUDE.md`,
  `CONTRIBUTING.md`, `OWNERS`, `OWNERS_ALIASES`, `DCO`, `SECURITY_CONTACTS`,
  `renovate.json`, `.markdownlint-cli2.yaml`, `.yamllint.yaml`, `vars.md`

`tools/vendor_import.py --check` treats their absence as expected and their
re-appearance as an error.

## Import and refresh (no git)

`tools/vendor_import.py` is the only supported importer. It downloads the
pinned archive (`https://codeload.github.com/metal3-io/metal3-dev-env/tar.gz/<commit>`)
or reads a local tarball/zip, rejects path traversal, strips the archive root,
copies the tree, writes `MANIFEST.sha256` over the **upstream** file list, then
prunes `PRUNE_PATHS` and records them in `VENDOR.json`.

```bash
python3 tools/vendor_import.py                      # pinned archive, default target
python3 tools/vendor_import.py --source ./m3de.tar.gz
python3 tools/vendor_import.py --check              # verify the tree
python3 tools/vendor_import.py --print-url          # print the pinned archive URL
python3 tools/vendor_import.py --reseal             # record the fork as the baseline
bash deploy/metal3-lab/run.sh restore               # upstream reference into vendor/*.upstream
bash deploy/metal3-lab/run.sh reseal                # same reseal, logged
```

`restore` never overwrites the fork: it materialises upstream next to it
(`vendor/metal3-dev-env.upstream`) so a change can be compared against the
pinned commit. Re-importing over the fork would restore scripts that call the
deleted libvirt playbooks.

`--reseal` recomputes `MANIFEST.sha256` over the current tree after a deliberate
edit, keeping the upstream commit as provenance, so `--check` reports new drift
instead of listing the edits already made on purpose. It refuses to reseal while
a pruned path exists or a required file is missing.

`--check` fails when the tree is missing, incomplete, or imported from another
commit. Files that differ from the manifest are reported as `local change:` and
are **not** failures: local edits are the point of vendoring. A pruned path that
exists again is reported as a problem, because it means the prune list and the
tree disagree.

## Editing rules

1. Keep entry points and paths stable (`01..04` scripts, `make clean`) —
   `deploy/metal3-lab/run.sh` invokes them by name.
2. Put project-specific behaviour behind new flags or new files rather than
   rewriting upstream lines that other tooling greps for.
3. Record every intentional divergence in `PATCHES.md` (file, reason, commit).
4. After a patch, run `python3 tools/vendor_import.py --check` and expect a
   `local change:` line for the edited file.
5. Re-importing with `restore` overwrites local patches — commit them first.
6. To drop more upstream code, add the path to `PRUNE_PATHS` instead of deleting
   it by hand: the next import must reproduce the same tree.

## Generated files

Upstream scripts write state into the tree they run in (`config_*.sh`,
`logs/`, `hack/tools/bin`). The upstream `.gitignore` shipped in this directory
covers those paths, so host state does not pollute `git status`. Lab artifacts
and logs go to `$METAL3_LAB_WORK_DIR` (default `.metal3-lab/`), which is
git-ignored.

## License and provenance

Upstream is Apache-2.0; the `LICENSE` file is vendored with the code. This
notice documents provenance only. It is not a runtime dependency: no network
call, no git command, and no upstream release schedule affects this repository.
