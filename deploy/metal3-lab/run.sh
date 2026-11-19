#!/usr/bin/env bash
set -Eeuo pipefail

# The lab runs vendored source: the upstream Metal3 dev-env tree lives in this
# repository under deploy/metal3-lab/vendor/metal3-dev-env and is executed in
# place. No remote clone, fetch, checkout or submodule is performed at run time, so
# the code can be edited, reviewed and re-developed locally like any other file.
readonly PINNED_COMMIT="7076f07624cdfba990f83bea24f749b37e3baad7"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
readonly VENDOR_DIR="${METAL3_LAB_VENDOR_DIR:-${SCRIPT_DIR}/vendor/metal3-dev-env}"
readonly WORK_DIR="${METAL3_LAB_WORK_DIR:-${REPO_ROOT}/.metal3-lab}"
readonly LOG_DIR="${WORK_DIR}/artifacts"
readonly VENDOR_TOOL="${REPO_ROOT}/tools/vendor_import.py"
readonly ACTION="${1:-}"

log() {
    printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

die() {
    log "ERROR: $*"
    exit 1
}

require_host_mutation_consent() {
    [[ "${METAL3_LAB_ALLOW_HOST_MUTATION:-}" == "1" ]] ||
        die "This action changes host networking, packages, libvirt and Kubernetes state. Set METAL3_LAB_ALLOW_HOST_MUTATION=1 only inside a disposable Ubuntu VM."
}

verify_vendor() {
    [[ -d "${VENDOR_DIR}" ]] ||
        die "Vendored Metal3 dev-env is missing at ${VENDOR_DIR}. Materialise it without git via: python3 ${VENDOR_TOOL} (or --source <local tarball>)."
    command -v python3 >/dev/null || die "python3 is required to verify the vendored tree"
    local report
    report="$(python3 "${VENDOR_TOOL}" --check --target "${VENDOR_DIR}" --commit "${PINNED_COMMIT}" 2>&1)" ||
        die "Vendored tree failed verification: ${report}"
    if grep -q 'local change:' <<<"${report}"; then
        log "WARNING: the vendored tree contains local edits; running the edited code on purpose"
    fi
    printf '%s\n' "${report}" | sed -n '1,3p'
}

preflight() {
    command -v make >/dev/null || die "make is required"
    command -v python3 >/dev/null || die "python3 is required"
    command -v lscpu >/dev/null || die "lscpu is required"
    command -v free >/dev/null || die "free is required"
    command -v timeout >/dev/null || die "timeout is required"

    [[ -r /etc/os-release ]] || die "Cannot identify the host operating system"
    . /etc/os-release
    [[ "${ID:-}" == "ubuntu" ]] || die "The pinned Metal3 dev-env workflow is validated here only for Ubuntu hosts"

    local cpus memory_kib
    cpus="$(nproc)"
    memory_kib="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
    (( cpus >= 4 )) || die "At least 4 CPUs are required; found ${cpus}"
    (( memory_kib >= 8 * 1024 * 1024 )) || die "At least 8 GiB RAM is required"

    if [[ ! -e /dev/kvm && "${LIBVIRT_DOMAIN_TYPE:-kvm}" != "qemu" ]]; then
        die "/dev/kvm is unavailable. Enable nested virtualization or explicitly set LIBVIRT_DOMAIN_TYPE=qemu (experimental upstream mode)."
    fi

    mkdir -p "${LOG_DIR}"
    log "Host preflight passed: Ubuntu ${VERSION_ID:-unknown}, CPUs=${cpus}, RAM_KiB=${memory_kib}, KVM=$([[ -e /dev/kvm ]] && echo available || echo unavailable)"
    log "Vendored Metal3 dev-env: ${VENDOR_DIR} (pinned ${PINNED_COMMIT})"
}

run_logged() {
    local name="$1"
    shift
    local log_file="${LOG_DIR}/${name}-$(date -u +%Y%m%dT%H%M%SZ).log"
    log "Running: $* (log: ${log_file})"
    set +e
    "$@" 2>&1 | tee "${log_file}"
    local status=${PIPESTATUS[0]}
    set -e
    if (( status != 0 )); then
        die "${name} failed with exit code ${status}; inspect ${log_file}"
    fi
}

in_vendor() {
    local name="$1"
    shift
    run_logged "${name}" bash -lc "cd '${VENDOR_DIR}' && $*"
}

prepare() {
    require_host_mutation_consent
    preflight
    verify_vendor
    export CAPM3_VERSION="${CAPM3_VERSION:-v1beta1}"
    export CAPI_VERSION="${CAPI_VERSION:-v1beta2}"
    export IMAGE_OS="${IMAGE_OS:-ubuntu}"
    [[ "${IMAGE_OS}" == "ubuntu" ]] || die "This runner currently supports IMAGE_OS=ubuntu only"
    in_vendor prepare-host "./01_prepare_host.sh"
    in_vendor configure-host "./02_configure_host.sh"
    in_vendor launch-management-cluster "./03_launch_mgmt_cluster.sh"
    in_vendor verify-management-cluster "./04_verify.sh"
}

verify() {
    preflight
    verify_vendor
    in_vendor verify-management-cluster "./04_verify.sh"
}

lifecycle() {
    require_host_mutation_consent
    preflight
    verify_vendor
    in_vendor lifecycle "make test"
}

clean() {
    require_host_mutation_consent
    preflight
    verify_vendor
    in_vendor cleanup "make clean"
}

restore() {
    # Development-only helper: re-materialise the pinned upstream tree from its
    # own archive, never from a git remote. Local edits are replaced on purpose.
    preflight
    run_logged restore-vendor python3 "${VENDOR_TOOL}" --target "${VENDOR_DIR}" --commit "${PINNED_COMMIT}"
}

case "${ACTION}" in
    prepare)
        prepare
        ;;
    verify)
        verify
        ;;
    lifecycle)
        lifecycle
        ;;
    clean)
        clean
        ;;
    restore)
        restore
        ;;
    *)
        cat <<'USAGE'
Usage: bash deploy/metal3-lab/run.sh <prepare|verify|lifecycle|clean|restore>

Actions:
  prepare    Run the vendored 01/02/03/04 scripts to install the host, configure
             libvirt and networking, launch the management cluster and verify it.
  verify     Run the vendored 04_verify.sh against the existing environment.
  lifecycle  Run the vendored `make test` provision/pivot/re-provision lifecycle.
  clean      Run the vendored `make clean`.
  restore    Re-materialise the pinned upstream tree from its archive (no git).

Vendored source:
  The upstream Metal3 dev-env tree is repository code under
  deploy/metal3-lab/vendor/metal3-dev-env. Edit it directly; the runner
  executes it in place and never clones, fetches or checks out upstream.

Safety:
  prepare, lifecycle and clean require METAL3_LAB_ALLOW_HOST_MUTATION=1.
  Run host-mutating actions only inside a disposable Ubuntu VM.
  Set METAL3_LAB_WORK_DIR to choose an artifact directory outside the repository.
  Set LIBVIRT_DOMAIN_TYPE=qemu only when KVM/nested virtualization is unavailable;
  upstream documents QEMU mode as experimental.
USAGE
        exit 2
        ;;
esac
