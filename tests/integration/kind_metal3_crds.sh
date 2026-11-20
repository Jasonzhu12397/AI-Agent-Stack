#!/usr/bin/env bash
# Staged integration check: install the shipped Metal3 IPAM and SR-IOV CRDs into
# a real Kubernetes API server (kind) and validate that they accept the manifests
# this platform renders. This exercises CRD schemas, not a physical deployment.
set -euo pipefail

KUBECTL="${KUBECTL:-kubectl}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IPAM_MANIFEST="${ROOT_DIR}/deploy/offline/addons/control-stack/ipam.yaml"
SRIOV_MANIFEST="${ROOT_DIR}/deploy/offline/addons/sriov-network-operator/operator.yaml"

for manifest in "${IPAM_MANIFEST}" "${SRIOV_MANIFEST}"; do
  if [ ! -f "${manifest}" ]; then
    echo "Missing manifest: ${manifest}" >&2
    exit 1
  fi
done

CERT_MANAGER_VERSION="${CERT_MANAGER_VERSION:-v1.16.3}"
CERT_MANAGER_URL="https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.yaml"

for namespace in capm3-system sriov-network-operator; do
  "${KUBECTL}" create namespace "${namespace}" --dry-run=client -o yaml \
    | "${KUBECTL}" apply -f -
done

echo "Installing cert-manager ${CERT_MANAGER_VERSION} (IPAM webhook prerequisite)"
"${KUBECTL}" apply -f "${CERT_MANAGER_URL}"
"${KUBECTL}" wait --for condition=established --timeout=120s \
  crd/certificates.cert-manager.io crd/issuers.cert-manager.io
"${KUBECTL}" rollout status deployment/cert-manager-webhook -n cert-manager --timeout=180s
"${KUBECTL}" rollout status deployment/cert-manager-cainjector -n cert-manager --timeout=180s

echo "Applying Metal3 IPAM manifests"
"${KUBECTL}" apply -f "${IPAM_MANIFEST}"

echo "Applying SR-IOV CRDs first, then the operator resources"
# kubectl discovery cannot resolve SriovOperatorConfig in the same batch that
# creates its CRD, so mirror the production installer's two-phase apply.
split_dir="$(mktemp -d)"
trap 'rm -rf "${split_dir}"' EXIT
awk -v dir="${split_dir}" '
  BEGIN { n = 0; file = sprintf("%s/doc-%03d.yaml", dir, n) }
  /^--- *$/ { n++; file = sprintf("%s/doc-%03d.yaml", dir, n); next }
  { print > file }
' "${SRIOV_MANIFEST}"
: > "${split_dir}/crds.yaml"
: > "${split_dir}/resources.yaml"
for doc in "${split_dir}"/doc-*.yaml; do
  [ -s "${doc}" ] || continue
  kind="$(awk '/^kind:/ {print $2; exit}' "${doc}")"
  if [ "${kind}" = "CustomResourceDefinition" ]; then
    if [ -s "${split_dir}/crds.yaml" ]; then echo "---" >> "${split_dir}/crds.yaml"; fi
    cat "${doc}" >> "${split_dir}/crds.yaml"
  else
    if [ -s "${split_dir}/resources.yaml" ]; then echo "---" >> "${split_dir}/resources.yaml"; fi
    cat "${doc}" >> "${split_dir}/resources.yaml"
  fi
done
"${KUBECTL}" apply -f "${split_dir}/crds.yaml"

echo "Waiting for required CRDs"
for crd in \
  ippools.ipam.metal3.io \
  ipaddresses.ipam.metal3.io \
  ipclaims.ipam.metal3.io \
  sriovnetworknodepolicies.sriovnetwork.openshift.io; do
  "${KUBECTL}" wait --for condition=established --timeout=120s "crd/${crd}"
done

"${KUBECTL}" apply -f "${split_dir}/resources.yaml"

echo "Server-side validation of platform-rendered SriovNetworkNodePolicy"
RENDERED_POLICY="${split_dir}/rendered-sriov-policy.yaml"
PYTHONPATH="${ROOT_DIR}/packages" python3 - "${RENDERED_POLICY}" <<'PY'
import sys
from pathlib import Path

import yaml

from agent_contracts.infra_networks import (
    plan_infra_networks,
    render_sriov_node_policies,
)

profile = {
    "networks": [
        {
            "name": "ci-control",
            "role": "control",
            "vlan": 100,
            "mtu": 1500,
            "cidr": "10.0.0.0/24",
            "gateway_ipv4": "10.0.0.1",
            "ip_pools": [{"start": "10.0.0.10", "end": "10.0.0.50"}],
            "interface": "eno1",
            "host_roles": ["control_plane", "worker"],
        },
        {
            "name": "ci-data",
            "role": "data",
            "vlan": 200,
            "mtu": 9000,
            "cidr": "10.10.0.0/24",
            "gateway_ipv4": "10.10.0.1",
            "ip_pools": [{"start": "10.10.0.10", "end": "10.10.0.50"}],
            "interface": "eno2",
            "host_roles": ["worker"],
        },
        {
            "name": "ci-storage",
            "role": "storage-internal",
            "vlan": 300,
            "mtu": 9000,
            "cidr": "10.20.0.0/24",
            "gateway_ipv4": "10.20.0.1",
            "ip_pools": [{"start": "10.20.0.10", "end": "10.20.0.50"}],
            "interface": "eno3",
            "host_roles": ["storage"],
        },
        {
            "name": "ci-dpdk",
            "role": "sriov",
            "vlan": 3400,
            "mtu": 9000,
            "interface": "ens3f0",
            "pci_addresses": ["0000:03:00.0"],
            "num_vfs": 8,
            "host_roles": ["worker"],
        }
    ]
}
documents = render_sriov_node_policies(plan_infra_networks(profile))
Path(sys.argv[1]).write_text(yaml.safe_dump_all(documents, sort_keys=False))
PY
"${KUBECTL}" apply --dry-run=server -f "${RENDERED_POLICY}"

echo "Metal3 and SR-IOV CRDs are present and accept rendered manifests"
