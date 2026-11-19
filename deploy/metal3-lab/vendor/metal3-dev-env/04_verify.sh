#!/usr/bin/env bash
# Physical machines (local patch: see vendor/PATCHES.md)
#
# Upstream verified this environment by matching text out of the cluster CLI,
# by listing virtual machine domains and by grepping the container runtime.
# All of that is gone: there are no virtual machines here, and no judgement
# about the cluster is made in shell.
#
# This script now does exactly one thing: hand the environment to
# tools/metal3_deploy.py, which asks the agent's observe_baremetalhosts skill
# about the physical hosts and apps/eph_agent/metal3_readiness.py about the
# management cluster (CRDs, controllers, replica sets, host bridges, infra
# containers, Ironic authentication). The exit code is the verdict.

set -u

# shellcheck disable=SC1091
source lib/logging.sh
# shellcheck disable=SC1091
source lib/common.sh
# shellcheck disable=SC1091
source lib/network.sh
# shellcheck disable=SC1091
source lib/images.sh
# shellcheck disable=SC1091
source lib/releases.sh

FAILS=0

# shellcheck disable=SC2329
check_bmhs_baremetal(){
  local FAILS_CHECK="${FAILS}"
  local tool="${METAL3_LAB_DEPLOY_TOOL:-}"
  local hosts_dir="${BARE_METAL_HOSTS_DIR:-${WORKING_DIR}/hosts}"

  RESULT_STR="Agent verified the Baremetalhosts and the management cluster"
  if [[ -z "${tool}" ]]; then
    echo >&2 "ERROR: METAL3_LAB_DEPLOY_TOOL is unset; it must point to tools/metal3_deploy.py"
    FAILS=$((FAILS+1))
    return 1
  fi

  python3 "${tool}" verify --dir "${hosts_dir}" --namespace metal3
  process_status $?

  echo ""
  return "$((FAILS-FAILS_CHECK))"
}

KUBECONFIG="${KUBECONFIG:-${HOME}/.kube/config}"
export KUBECONFIG
EXPTD_CONTAINERS="httpd-infra registry"

check_bmhs_baremetal

echo -e "\nNumber of failures : $FAILS"
exit "${FAILS}"
