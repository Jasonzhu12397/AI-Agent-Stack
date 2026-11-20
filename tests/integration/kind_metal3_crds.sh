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

for namespace in capm3-system sriov-network-operator metal3; do
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
# The IPPool admission webhook must actually answer: a dry-run that skips it
# would only prove the structural schema, not that real admission accepts the
# platform's rendered pools.
echo "Waiting for the IPAM webhook to serve requests"
"${KUBECTL}" wait --for condition=Ready --timeout=180s \
  certificate/ipam-serving-cert -n capm3-system
"${KUBECTL}" rollout status deployment/ipam-controller-manager \
  -n capm3-system --timeout=240s

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

echo "Server-side validation of platform-rendered IPPool and SriovNetworkNodePolicy"
RENDERED_POLICY="${split_dir}/rendered-sriov-policy.yaml"
RENDERED_IPPOOL="${split_dir}/rendered-ippool.yaml"
python3 - "${RENDERED_POLICY}" "${RENDERED_IPPOOL}" "${ROOT_DIR}" <<'PY'
import importlib.util
import sys
from pathlib import Path

import yaml

module_path = Path(sys.argv[3]) / "packages/agent_contracts/infra_networks.py"
module_spec = importlib.util.spec_from_file_location("infra_networks_contract", module_path)
contract = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(contract)
plan_infra_networks = contract.plan_infra_networks
render_sriov_node_policies = contract.render_sriov_node_policies
render_ip_pools = contract.render_ip_pools

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
plan = plan_infra_networks(profile)
Path(sys.argv[1]).write_text(
    yaml.safe_dump_all(render_sriov_node_policies(plan), sort_keys=False)
)
Path(sys.argv[2]).write_text(
    yaml.safe_dump_all(
        render_ip_pools(plan, cluster_name="ci-cluster"),
        sort_keys=False,
    )
)
PY
"${KUBECTL}" apply --dry-run=server -f "${RENDERED_IPPOOL}"
"${KUBECTL}" apply --dry-run=server -f "${RENDERED_POLICY}"

echo "Metal3 and SR-IOV CRDs are present and accept rendered manifests"

echo "Server-side validation of every vendored control-stack manifest"
# Server-side dry-run never persists a Namespace object, so namespaced
# resources would fail even though a real apply creates the namespace first.
for namespace in \
  capm3-system \
  capi-system \
  capi-kubeadm-bootstrap-system \
  capi-kubeadm-control-plane-system \
  baremetal-operator-system \
  ironic-standalone-operator-system; do
  "${KUBECTL}" create namespace "${namespace}" --dry-run=client -o yaml \
    | "${KUBECTL}" apply -f - >/dev/null
done
for manifest in "${ROOT_DIR}"/deploy/offline/addons/control-stack/*.yaml; do
  echo "  $(basename "${manifest}")"
  "${KUBECTL}" apply --dry-run=server -f "${manifest}" >/dev/null
done
echo "All control-stack manifests are accepted by the API server"

echo "Server-side validation of the platform-rendered Ironic CR"
"${KUBECTL}" create namespace baremetal-operator-system --dry-run=client -o yaml \
  | "${KUBECTL}" apply -f - >/dev/null
python3 - "${ROOT_DIR}" "${split_dir}/ironic-crd.yaml" <<'PY'
import sys
from pathlib import Path

import yaml

root = Path(sys.argv[1])
documents = list(
    yaml.safe_load_all(
        (root / "deploy/offline/addons/control-stack/ironic.yaml").read_text(
            encoding="utf-8"
        )
    )
)
crd = [
    document
    for document in documents
    if document
    and document.get("kind") == "CustomResourceDefinition"
    and document["spec"]["names"]["kind"] == "Ironic"
]
Path(sys.argv[2]).write_text(yaml.safe_dump_all(crd, sort_keys=False))
PY
"${KUBECTL}" apply -f "${split_dir}/ironic-crd.yaml" >/dev/null
"${KUBECTL}" wait --for condition=established --timeout=120s \
  crd/ironics.ironic.metal3.io
python3 - "${ROOT_DIR}" "${split_dir}/rendered-ironic-cr.yaml" <<'PY'
import importlib.util
import sys
from pathlib import Path

import yaml

spec = importlib.util.spec_from_file_location(
    "ironic_cr_contract",
    Path(sys.argv[1]) / "apps/eph_agent/ironic_cr.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
document = module.render_ironic_cr(
    {
        "ironic": {
            "api_credentials_name": "ironic-auth",
            "version": "37.0",
            "images": {
                "ironic": "registry.local/ironic:37.0",
                "deploy_ramdisk_downloader": "registry.local/ipa-downloader:latest",
                "keepalived": "registry.local/keepalived:2.2.4",
            },
            "networking": {
                "interface": "eno2",
                "ip_address": "10.130.60.84",
                "image_server_port": 8080,
            },
        }
    }
)
Path(sys.argv[2]).write_text(yaml.safe_dump(document, sort_keys=False))
PY
"${KUBECTL}" apply --dry-run=server -f "${split_dir}/rendered-ironic-cr.yaml"
