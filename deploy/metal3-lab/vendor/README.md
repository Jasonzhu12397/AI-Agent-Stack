# Vendored Metal3 dev-env

This directory is **repository code**. The upstream `metal3-io/metal3-dev-env`
tree at commit `7076f07624cdfba990f83bea24f749b37e3baad7` was copied into
`deploy/metal3-lab/vendor/metal3-dev-env/` so that:

- the lab runner never clones, fetches or checks out a git remote at run time;
- the scripts, libs and Ansible playbooks can be edited, reviewed and extended
  in-tree like any other file of this repository;
- behaviour fixes do not wait for an upstream release: change the code here.

## What is inside

| Path | Role |
|---|---|
| `01_prepare_host.sh` | host package install, Ansible bootstrap |
| `02_configure_host.sh` | libvirt, bridge, firewall, virtual BMC |
| `03_launch_mgmt_cluster.sh` | kind management cluster, ironic/bmo/capi deploy |
| `04_verify.sh` | provision/verify the management cluster |
| `lib/*.sh` | shared helpers (common, network, download, releases, TLS, auth) |
| `vm-setup/` | Ansible playbooks, roles, templates, inventory |
| `Makefile`, `ansible.cfg`, `config_example.sh` | upstream driver entry points |
| `VENDOR.json` | provenance: upstream repo, commit, import source, file count |
| `MANIFEST.sha256` | per-file digest list used for integrity checks |

`VENDOR.json` and `MANIFEST.sha256` are generated; every other file is upstream
source kept verbatim, except for deliberate local patches made by this project.

## Import and refresh (no git)

`tools/vendor_import.py` is the only supported importer. It downloads the
pinned archive (`https://codeload.github.com/metal3-io/metal3-dev-env/tar.gz/<commit>`)
or reads a local tarball/zip, rejects path traversal, strips the archive root,
copies the tree, then writes `VENDOR.json` and `MANIFEST.sha256`.

```bash
python3 tools/vendor_import.py                      # pinned archive, default target
python3 tools/vendor_import.py --source ./m3de.tar.gz
python3 tools/vendor_import.py --check              # verify the tree
python3 tools/vendor_import.py --print-url          # print the pinned archive URL
bash deploy/metal3-lab/run.sh restore               # same import, logged
```

`--check` fails when the tree is missing, incomplete, or imported from another
commit. Files that differ from the manifest are reported as `local change:` and
are **not** failures: local edits are the point of vendoring. `run.sh` logs them
as a warning before executing the edited code.

## Editing rules

1. Keep entry points and paths stable (`01..04` scripts, `make test`,
   `make clean`) — `deploy/metal3-lab/run.sh` invokes them by name.
2. Put project-specific behaviour behind new flags or new files rather than
   rewriting upstream lines that other tooling greps for.
3. Record every intentional divergence in `PATCHES.md` (file, reason, commit).
4. After a patch, run `python3 tools/vendor_import.py --check` and expect a
   `local change:` line for the edited file.
5. Re-importing with `restore` overwrites local patches — commit them first.

## Generated files

Upstream scripts write state into the tree they run in (`config_*.sh`,
`bmhosts_crs.yaml`, `logs/`, `hack/tools/bin`, `vm-setup/roles/run_tests/files`).
The upstream `.gitignore` shipped in this directory already covers those paths,
so host state does not pollute `git status`. Lab artifacts and logs go to
`$METAL3_LAB_WORK_DIR` (default `.metal3-lab/`), which is git-ignored.

## License and provenance

Upstream is Apache-2.0; the `LICENSE` file is vendored with the code. This
notice documents provenance only. It is not a runtime dependency: no network
call, no git command, and no upstream release schedule affects this repository.
