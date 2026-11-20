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

echo "Applying Metal3 IPAM manifests"
"${KUBECTL}" apply -f "${IPAM_MANIFEST}"

echo "Applying SR-IOV Network Operator manifests"
"${KUBECTL}" apply -f "${SRIOV_MANIFEST}"

echo "Waiting for required CRDs"
for crd in \
  ippools.ipam.metal3.io \
  ipaddresses.ipam.metal3.io \
  ipclaims.ipam.metal3.io \
  sriovnetworknodepolicies.sriovnetwork.openshift.io; do
  "${KUBECTL}" wait --for condition=established --timeout=120s "crd/${crd}"
done

"${KUBECTL}" create namespace sriov-network-operator --dry-run=client -o yaml \
  | "${KUBECTL}" apply -f -

echo "Server-side validation of a rendered policy shape"
cat <<'YAML' | "${KUBECTL}" apply --dry-run=server -f -
apiVersion: sriovnetwork.openshift.io/v1
kind: SriovNetworkNodePolicy
metadata:
  name: ci-dry-run
  namespace: sriov-network-operator
spec:
  nodeSelector:
    feature.node.kubernetes.io/network-sriov.capable: "true"
  numVfs: 8
  nicSelector:
    pciAddresses:
      - "0000:03:00.0"
  deviceType: netdevice
  isRdma: false
  resourceName: ci-dry-run
YAML

echo "Metal3 and SR-IOV CRDs are present and accept rendered manifests"
