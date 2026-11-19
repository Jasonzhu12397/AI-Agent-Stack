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
readonly BMH_TOOL="${REPO_ROOT}/tools/bmh_from_inventory.py"
readonly AGENT_BRIDGE="${REPO_ROOT}/tools/metal3_agent_bridge.py"
readonly DEPLOY_TOOL="${REPO_ROOT}/tools/metal3_deploy.py"
readonly CLUSTER_NAME="${METAL3_LAB_CLUSTER_NAME:-target-cluster}"
readonly CONTROL_PLANE_COUNT="${METAL3_LAB_CONTROL_PLANE_COUNT:-3}"
# Platform selects which half of the vendored tree is exercised:
#   baremetal (default) - the BMHs describe physical machines, no VM is created
#   libvirt             - upstream demo mode, virtual bare metal hosts on the host
readonly PLATFORM="${METAL3_LAB_PLATFORM:-baremetal}"
readonly INVENTORY="${METAL3_LAB_INVENTORY:-${SCRIPT_DIR}/hosts/baremetal-nodes.yaml}"
readonly HOSTS_DIR="${METAL3_LAB_HOSTS_DIR:-${WORK_DIR}/hosts}"
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

    if [[ "${PLATFORM}" == "baremetal" ]]; then
        log "Platform: baremetal (physical machines, no libvirt, no vbmc/sushy)"
    elif [[ ! -e /dev/kvm && "${LIBVIRT_DOMAIN_TYPE:-kvm}" != "qemu" ]]; then
        die "/dev/kvm is unavailable. Enable nested virtualization or explicitly set LIBVIRT_DOMAIN_TYPE=qemu (experimental upstream mode), or switch to METAL3_LAB_PLATFORM=baremetal for physical hardware."
    fi

    [[ "${PLATFORM}" == "baremetal" || "${PLATFORM}" == "libvirt" ]] ||
        die "METAL3_LAB_PLATFORM must be baremetal or libvirt; got ${PLATFORM}"

    mkdir -p "${LOG_DIR}"
    log "Host preflight passed: Ubuntu ${VERSION_ID:-unknown}, CPUs=${cpus}, RAM_KiB=${memory_kib}, KVM=$([[ -e /dev/kvm ]] && echo available || echo unavailable)"
    log "Vendored Metal3 dev-env: ${VENDOR_DIR} (pinned ${PINNED_COMMIT})"
}

# Export what the vendored scripts read. NODES_PLATFORM is the upstream
# extension point: baremetal adds a third value next to libvirt and fake.
platform_env() {
    export NODES_PLATFORM="${PLATFORM}"
    # BMH registration and readiness judgement run through the agent bridge,
    # never through kubectl calls embedded in the vendored scripts.
    export METAL3_LAB_AGENT_BRIDGE="${AGENT_BRIDGE}"
    export METAL3_LAB_DEPLOY_TOOL="${DEPLOY_TOOL}"
    export METAL3_LAB_BMH_DRIVER="${METAL3_LAB_BMH_DRIVER:-agent}"
    if [[ "${PLATFORM}" == "baremetal" ]]; then
        export BARE_METAL_HOSTS_DIR="${HOSTS_DIR}"
    fi
    log "NODES_PLATFORM=${NODES_PLATFORM}${BARE_METAL_HOSTS_DIR:+ BARE_METAL_HOSTS_DIR=${BARE_METAL_HOSTS_DIR}} BMH_DRIVER=${METAL3_LAB_BMH_DRIVER}"
}

# Build the target cluster plan and the manifests the agent will apply.
#
# No shell parsing happens here: tools/metal3_deploy.py reads the platform
# fleet, reasons about which machines are control plane and which are data
# plane workers, renders the BMHs through the EPH agent's own template and
# records the reasoning in plan.json. This function only decides where the
# output goes and how many machines the plan owns.
render_bmh_manifests() {
    [[ "${PLATFORM}" == "baremetal" ]] || return 0
    command -v python3 >/dev/null || die "python3 is required to render BMH manifests"

    local -a args=(plan --cluster-name "${CLUSTER_NAME}" --namespace metal3 --out-dir "${HOSTS_DIR}")
    args+=(--control-plane-count "${CONTROL_PLANE_COUNT}")
    [[ -n "${METAL3_LAB_PROJECT_ID:-}" ]] && args+=(--project "${METAL3_LAB_PROJECT_ID}")
    [[ -n "${METAL3_LAB_CONTROL_PLANE_ENDPOINT:-}" ]] && args+=(--control-plane-endpoint "${METAL3_LAB_CONTROL_PLANE_ENDPOINT}")
    [[ -n "${METAL3_LAB_IMAGE_URL:-}" ]] && args+=(--image-url "${METAL3_LAB_IMAGE_URL}")
    [[ -n "${METAL3_LAB_K8S_VERSION:-}" ]] && args+=(--k8s-version "${METAL3_LAB_K8S_VERSION}")
    [[ -n "${METAL3_LAB_DATA_TEMPLATE:-}" ]] && args+=(--data-template "${METAL3_LAB_DATA_TEMPLATE}")
    [[ -n "${METAL3_LAB_DJANGO_SETTINGS:-}" ]] && args+=(--django-settings "${METAL3_LAB_DJANGO_SETTINGS}")

    log "Planning ${CLUSTER_NAME} from the platform fleet"
    run_logged plan-${CLUSTER_NAME} python3 "${DEPLOY_TOOL}" "${args[@]}"
    NUM_NODES="$(python3 "${DEPLOY_TOOL}" count --dir "${HOSTS_DIR}")"
    export NUM_NODES
    (( NUM_NODES >= 1 )) || die "The plan assigned no machine; check the platform inventory report"
    log "Planned ${NUM_NODES} machine(s) for ${CLUSTER_NAME}"
}

prepare() {
    require_host_mutation_consent
    preflight
    verify_vendor
    platform_env
    export CAPM3_VERSION="${CAPM3_VERSION:-v1beta1}"
    export CAPI_VERSION="${CAPI_VERSION:-v1beta2}"
    export IMAGE_OS="${IMAGE_OS:-ubuntu}"
    [[ "${IMAGE_OS}" == "ubuntu" ]] || die "This runner currently supports IMAGE_OS=ubuntu only"
    render_bmh_manifests
    in_vendor prepare-host "./01_prepare_host.sh"
    in_vendor configure-host "./02_configure_host.sh"
    in_vendor launch-management-cluster "./03_launch_mgmt_cluster.sh"
    in_vendor verify-management-cluster "./04_verify.sh"
}

verify() {
    preflight
    verify_vendor
    platform_env
    in_vendor verify-management-cluster "./04_verify.sh"
}

lifecycle() {
    require_host_mutation_consent
    preflight
    verify_vendor
    platform_env
    in_vendor lifecycle "make test"
}

clean() {
    require_host_mutation_consent
    preflight
    verify_vendor
    platform_env
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
             networking, launch the management cluster and verify it.
  verify     Run the vendored 04_verify.sh against the existing environment.
  lifecycle  Run the vendored `make test` provision/pivot/re-provision lifecycle.
  clean      Run the vendored `make clean`.
  restore    Re-materialise the pinned upstream tree from its archive (no git).

Platforms:
  METAL3_LAB_PLATFORM=baremetal (default)
      Physical machines. The inventory at deploy/metal3-lab/hosts/baremetal-nodes.yaml
      is rendered into BMH manifests by tools/bmh_from_inventory.py, then applied.
      No libvirt, no vbmc, no sushy-tools, no VM is created and /dev/kvm is not required.
  METAL3_LAB_PLATFORM=libvirt
      Upstream demo mode: the vendored vm-setup roles create virtual bare metal
      hosts on this host, with vbmc/sushy-tools emulating their BMCs.

Bare metal inputs:
  METAL3_LAB_PROJECT_ID  Platform project to read the fleet from.
  METAL3_LAB_CLUSTER_NAME         Target cluster name (default target-cluster).
  METAL3_LAB_CONTROL_PLANE_COUNT  Control plane size: 1, 3, 5 or 7 (default 3).
  METAL3_LAB_CONTROL_PLANE_ENDPOINT  VIP or DNS name of the target API server.
  METAL3_LAB_IMAGE_URL   Live-OS image the machines boot into.
  METAL3_LAB_DATA_TEMPLATE       Metal3DataTemplate YAML for the machines.
  METAL3_LAB_HOSTS_DIR   Where the plan and manifests land
                         (default .metal3-lab/hosts).

Planning:
  tools/metal3_deploy.py reads AgentMachine rows (BMC endpoint, credential
  reference, reported hardware) and reasons about roles: explicit
  ai-agent-stack.io/role labels win, otherwise machines are ranked by reported
  CPU, RAM, disk and identity status. Every decision is recorded with its
  reason in plan.json; machines with unclear facts are flagged, never guessed.

Vendored source:
  The upstream Metal3 dev-env tree is repository code under
  deploy/metal3-lab/vendor/metal3-dev-env. Edit it directly; the runner
  executes it in place and never clones, fetches or checks out upstream.
  Local edits to the vendored tree are registered in vendor/PATCHES.md.

Safety:
  prepare, lifecycle and clean require METAL3_LAB_ALLOW_HOST_MUTATION=1.
  Run host-mutating actions only on a host you are willing to reconfigure:
  they install packages, rewrite bridges and firewall rules.
  Set METAL3_LAB_WORK_DIR to choose an artifact directory outside the repository.
  Set LIBVIRT_DOMAIN_TYPE=qemu only in libvirt mode when KVM is unavailable;
  upstream documents QEMU mode as experimental.
USAGE
        exit 2
        ;;
esac
