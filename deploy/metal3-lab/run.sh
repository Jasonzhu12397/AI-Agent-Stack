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
readonly AGENT_BRIDGE="${REPO_ROOT}/tools/metal3_agent_bridge.py"
readonly DEPLOY_TOOL="${REPO_ROOT}/tools/metal3_deploy.py"
readonly CLUSTER_NAME="${METAL3_LAB_CLUSTER_NAME:-target-cluster}"
readonly CONTROL_PLANE_COUNT="${METAL3_LAB_CONTROL_PLANE_COUNT:-3}"
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
        die "This action changes host networking, packages and Kubernetes state. Set METAL3_LAB_ALLOW_HOST_MUTATION=1 only on a host you are willing to reconfigure."
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
    if grep -q 'intentionally not vendored:' <<<"${report}"; then
        log "Vendored tree is pruned: upstream virtualisation, VM e2e and dev tooling are not vendored"
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

    # Physical machines only: no /dev/kvm, no libvirt, no nested virtualization.
    if [[ -e /dev/kvm ]]; then
        log "Note: /dev/kvm exists but is unused; this lab never starts a virtual machine"
    fi

    mkdir -p "${LOG_DIR}"
    log "Host preflight passed: Ubuntu ${VERSION_ID:-unknown}, CPUs=${cpus}, RAM_KiB=${memory_kib}"
    log "Vendored Metal3 dev-env: ${VENDOR_DIR} (pinned ${PINNED_COMMIT})"
}

# Export what the vendored scripts read.
platform_env() {
    # BMH registration and readiness judgement run through the agent bridge,
    # never through kubectl calls embedded in the vendored scripts.
    export METAL3_LAB_AGENT_BRIDGE="${AGENT_BRIDGE}"
    export METAL3_LAB_DEPLOY_TOOL="${DEPLOY_TOOL}"
    export METAL3_LAB_BMH_DRIVER="${METAL3_LAB_BMH_DRIVER:-agent}"
    export BARE_METAL_HOSTS_DIR="${HOSTS_DIR}"
    log "BARE_METAL_HOSTS_DIR=${BARE_METAL_HOSTS_DIR} BMH_DRIVER=${METAL3_LAB_BMH_DRIVER}"
}

# Build the target cluster plan and the manifests the agent will apply.
#
# No shell parsing happens here: tools/metal3_deploy.py reads the platform
# fleet, reasons about which machines are control plane and which are data
# plane workers, renders the BMHs through the EPH agent's own template and
# records the reasoning in plan.json. This function only decides where the
# output goes and how many machines the plan owns.
render_bmh_manifests() {
    command -v python3 >/dev/null || die "python3 is required to render BMH manifests"

    local -a args=(plan --cluster-name "${CLUSTER_NAME}" --namespace metal3 --out-dir "${HOSTS_DIR}")
    args+=(--control-plane-count "${CONTROL_PLANE_COUNT}")
    [[ -n "${METAL3_LAB_PROJECT_ID:-}" ]] && args+=(--project "${METAL3_LAB_PROJECT_ID}")
    [[ -n "${METAL3_LAB_CONTROL_PLANE_ENDPOINT:-}" ]] && args+=(--control-plane-endpoint "${METAL3_LAB_CONTROL_PLANE_ENDPOINT}")
    [[ -n "${METAL3_LAB_IMAGE_URL:-}" ]] && args+=(--image-url "${METAL3_LAB_IMAGE_URL}")
    [[ -n "${METAL3_LAB_K8S_VERSION:-}" ]] && args+=(--k8s-version "${METAL3_LAB_K8S_VERSION}")
    [[ -n "${METAL3_LAB_DATA_TEMPLATE:-}" ]] && args+=(--data-template "${METAL3_LAB_DATA_TEMPLATE}")
    [[ -n "${METAL3_LAB_DJANGO_SETTINGS:-}" ]] && args+=(--django-settings "${METAL3_LAB_DJANGO_SETTINGS}")
    [[ "${METAL3_LAB_WRITE_BACK:-0}" == "1" ]] && args+=(--write-back)

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

clean() {
    require_host_mutation_consent
    preflight
    verify_vendor
    platform_env
    in_vendor cleanup "make clean"
}

restore() {
    # Development-only helper: materialise the pinned UPSTREAM tree into a
    # scratch directory for comparison, never from a git remote and never over
    # the fork. The vendored tree in this repository is the code that runs; it
    # has no libvirt half and its scripts call python, so overwriting it with
    # upstream would break the lab.
    preflight
    local reference="${VENDOR_DIR}.upstream"
    run_logged restore-vendor python3 "${VENDOR_TOOL}" --target "${reference}" --commit "${PINNED_COMMIT}"
    log "Upstream reference tree: ${reference} (the lab keeps running ${VENDOR_DIR})"
}

reseal() {
    # After a deliberate edit to the vendored fork, record the new baseline so
    # --check reports future drift instead of the edits already made on purpose.
    command -v python3 >/dev/null || die "python3 is required"
    run_logged reseal-vendor python3 "${VENDOR_TOOL}" --reseal --target "${VENDOR_DIR}"
}

case "${ACTION}" in
    prepare)
        prepare
        ;;
    verify)
        verify
        ;;
    clean)
        clean
        ;;
    restore)
        restore
        ;;
    reseal)
        reseal
        ;;
    *)
        cat <<'USAGE'
Usage: bash deploy/metal3-lab/run.sh <prepare|verify|clean|restore|reseal>

Actions:
  prepare    Run the vendored 01/02/03/04 scripts to install the host, configure
             networking, launch the management cluster and verify it.
  verify     Run the vendored 04_verify.sh against the existing environment.
  clean      Run the vendored `make clean`.
  restore    Materialise the pinned upstream tree into vendor/*.upstream for
             comparison (no git). The forked tree that runs is never overwritten.
  reseal     Record the current vendored fork as the manifest baseline after a
             deliberate edit, so `verify` reports only new drift.

Platform:
  Physical machines only. The plan in deploy/metal3-lab/hosts (or in
  METAL3_LAB_HOSTS_DIR) is rendered from the platform fleet by
  tools/metal3_deploy.py, then applied through the agent. The vendored tree
  contains no libvirt roles, no vbmc/sushy-tools and no VM-based test suite:
  upstream's virtual half is deleted by tools/vendor_import.py on every import.

Bare metal inputs:
  METAL3_LAB_PROJECT_ID  Platform project to read the fleet from.
  METAL3_LAB_CLUSTER_NAME         Target cluster name (default target-cluster).
  METAL3_LAB_CONTROL_PLANE_COUNT  Control plane size: 1, 3, 5 or 7 (default 3).
  METAL3_LAB_CONTROL_PLANE_ENDPOINT  VIP or DNS name of the target API server.
  METAL3_LAB_IMAGE_URL   Live-OS image the machines boot into.
  METAL3_LAB_DATA_TEMPLATE       Metal3DataTemplate YAML for the machines.
  METAL3_LAB_HOSTS_DIR   Where the plan and manifests land
                         (default .metal3-lab/hosts).
  METAL3_LAB_WRITE_BACK 1 mirrors the plan into AgentMachine.bmh_name and
                         lifecycle_state and audits every assignment.

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
  prepare and clean require METAL3_LAB_ALLOW_HOST_MUTATION=1.
  Run host-mutating actions only on a host you are willing to reconfigure:
  they install packages, rewrite bridges and firewall rules.
  Set METAL3_LAB_WORK_DIR to choose an artifact directory outside the repository.
USAGE
        exit 2
        ;;
esac
