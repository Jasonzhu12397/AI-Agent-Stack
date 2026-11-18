#!/usr/bin/env bash
set -Eeuo pipefail

readonly UPSTREAM_REPO="https://github.com/metal3-io/metal3-dev-env.git"
readonly UPSTREAM_COMMIT="7076f07624cdfba990f83bea24f749b37e3baad7"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
readonly WORK_DIR="${METAL3_LAB_WORK_DIR:-${REPO_ROOT}/.metal3-lab}"
readonly UPSTREAM_DIR="${WORK_DIR}/metal3-dev-env"
readonly LOG_DIR="${WORK_DIR}/artifacts"
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

preflight() {
    command -v git >/dev/null || die "git is required"
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
}

checkout_upstream() {
    mkdir -p "${WORK_DIR}"
    if [[ ! -d "${UPSTREAM_DIR}/.git" ]]; then
        log "Cloning Metal3 dev-env"
        git clone --filter=blob:none "${UPSTREAM_REPO}" "${UPSTREAM_DIR}"
    else
        local origin
        origin="$(git -C "${UPSTREAM_DIR}" remote get-url origin)"
        [[ "${origin}" == "${UPSTREAM_REPO}" ]] ||
            die "Unexpected upstream origin: ${origin}"
    fi
    git -C "${UPSTREAM_DIR}" fetch --no-tags origin "${UPSTREAM_COMMIT}"
    git -C "${UPSTREAM_DIR}" checkout --detach "${UPSTREAM_COMMIT}"
    local actual
    actual="$(cd -- "${UPSTREAM_DIR}" && git rev-parse HEAD)"
    [[ "${actual}" == "${UPSTREAM_COMMIT}" ]] ||
        die "Upstream checkout mismatch: expected ${UPSTREAM_COMMIT}, got ${actual}"
    log "Pinned upstream checkout: ${actual}"
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

prepare() {
    require_host_mutation_consent
    preflight
    checkout_upstream
    export CAPM3_VERSION="${CAPM3_VERSION:-v1beta1}"
    export CAPI_VERSION="${CAPI_VERSION:-v1beta2}"
    export IMAGE_OS="${IMAGE_OS:-ubuntu}"
    [[ "${IMAGE_OS}" == "ubuntu" ]] || die "This runner currently supports IMAGE_OS=ubuntu only"
    run_logged prepare-host bash -lc "cd '${UPSTREAM_DIR}' && ./01_prepare_host.sh"
    run_logged configure-host bash -lc "cd '${UPSTREAM_DIR}' && ./02_configure_host.sh"
    run_logged launch-management-cluster bash -lc "cd '${UPSTREAM_DIR}' && ./03_launch_mgmt_cluster.sh"
    run_logged verify-management-cluster bash -lc "cd '${UPSTREAM_DIR}' && ./04_verify.sh"
}

verify() {
    preflight
    checkout_upstream
    run_logged verify-management-cluster bash -lc "cd '${UPSTREAM_DIR}' && ./04_verify.sh"
}

lifecycle() {
    require_host_mutation_consent
    preflight
    checkout_upstream
    run_logged upstream-capi-metal3-lifecycle bash -lc "cd '${UPSTREAM_DIR}' && make test"
}

clean() {
    require_host_mutation_consent
    preflight
    checkout_upstream
    run_logged cleanup bash -lc "cd '${UPSTREAM_DIR}' && make clean"
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
    *)
        cat <<'USAGE'
Usage: bash deploy/metal3-lab/run.sh <prepare|verify|lifecycle|clean>

Actions:
  prepare    Install/configure the upstream Metal3 dev environment and launch its management cluster.
  verify     Run the upstream dev-env verification against the existing environment.
  lifecycle  Run the upstream provision, pivot, re-pivot and deprovision lifecycle.
  clean      Run the upstream dev-env cleanup.

Safety:
  prepare, lifecycle and clean require METAL3_LAB_ALLOW_HOST_MUTATION=1.
  Run host-mutating actions only inside a disposable Ubuntu VM.
  Set METAL3_LAB_WORK_DIR to choose a workspace outside the repository.
  Set LIBVIRT_DOMAIN_TYPE=qemu only when KVM/nested virtualization is unavailable;
  upstream documents QEMU mode as experimental.
USAGE
        exit 2
        ;;
esac
