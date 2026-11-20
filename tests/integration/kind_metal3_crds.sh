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
    cat "${doc}" >> "${split_dir}/crds.yaml"
  else
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
