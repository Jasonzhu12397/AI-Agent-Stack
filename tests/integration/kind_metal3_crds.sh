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

echo "CRD-schema validation of the platform-rendered Ironic CR"
# Everything below is sent straight to the API server instead of through
# `kubectl apply`. kubectl's apply path prints the object it just created, and
# the printer for a custom resource reads the CRD's additionalPrinterColumns
# (`.status.conditions`) on an object that has no status yet, so a perfectly
# valid object can still abort with
#   error: .status.conditions accessor error: <nil> is of the type <nil>, ...
# `kubectl --validate=true` is no alternative either: for custom resources it
# silently skips validation, which the negative probe at the end detects.
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
if not crd:
    raise SystemExit("the vendored control stack does not define the Ironic CRD")
Path(sys.argv[2]).write_text(yaml.safe_dump_all(crd, sort_keys=False))
PY
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
python3 - "${split_dir}/ironic-crd.yaml" "${split_dir}/ironic-crd.json" <<'PY'
import json
import sys
from pathlib import Path

import yaml

documents = list(yaml.safe_load_all(Path(sys.argv[1]).read_text()))
Path(sys.argv[2]).write_text(json.dumps(documents[0]))
PY
python3 - "${split_dir}/rendered-ironic-cr.yaml" "${split_dir}/rendered-ironic-cr.json" <<'PY'
import json
import sys
from pathlib import Path

import yaml

document = yaml.safe_load(Path(sys.argv[1]).read_text())
Path(sys.argv[2]).write_text(json.dumps(document))
PY
cat > "${split_dir}/namespace.json" <<'JSON'
{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "baremetal-operator-system"}}
JSON

api_status() {
  # $1 endpoint path, $2 request body; prints the HTTP status code.
  curl -sS -o "${split_dir}/response.json" -w '%{http_code}' \
    -X POST "http://127.0.0.1:8010$1" \
    -H 'Content-Type: application/json' --data-binary @"$2"
}

api_get_status() {
  curl -sS -o "${split_dir}/get.json" -w '%{http_code}' "http://127.0.0.1:8010$1"
}

api_path_for() {
  python3 - "$1" <<'PY'
import json
import sys

document = json.loads(open(sys.argv[1]).read())
version = document["apiVersion"]
kind = document["kind"].lower()
namespace = document["metadata"].get("namespace")
group, _, api_version = version.partition("/")
resource = kind + "s"
if namespace:
    print(f"/apis/{group}/{api_version}/namespaces/{namespace}/{resource}?dryRun=All")
else:
    print(f"/apis/{group}/{api_version}/{resource}?dryRun=All")
PY
}

dry_run_status() {
  curl -sS -o "${split_dir}/dry-run-response.json" -w '%{http_code}' \
    -X POST "http://127.0.0.1:8010$(api_path_for "$1")" \
    -H 'Content-Type: application/json' --data-binary @"$1"
}

"${KUBECTL}" proxy --port=8010 >/dev/null 2>&1 &
proxy_pid=$!
trap 'kill "${proxy_pid}" 2>/dev/null; rm -rf "${split_dir}"' EXIT
ready=false
for _ in $(seq 1 80); do
  if curl -sf "http://127.0.0.1:8010/version" >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 0.25
done
if [ "${ready}" != true ]; then
  echo "kubectl proxy did not become ready" >&2
  exit 1
fi

echo "  namespace baremetal-operator-system"
namespace_status=$(api_status /api/v1/namespaces "${split_dir}/namespace.json")
case "${namespace_status}" in
  200|201|409) ;;
  *)
    cat "${split_dir}/response.json" >&2
    echo "The API server rejected the workspace namespace (HTTP ${namespace_status})" >&2
    exit 1
    ;;
esac

echo "  Ironic CRD"
crd_status=$(api_status /apis/apiextensions.k8s.io/v1/customresourcedefinitions \
  "${split_dir}/ironic-crd.json")
case "${crd_status}" in
  200|201|409) ;;
  *)
    cat "${split_dir}/response.json" >&2
    echo "The API server rejected the Ironic CRD (HTTP ${crd_status})" >&2
    exit 1
    ;;
esac
established=false
for _ in $(seq 1 120); do
  if [ "$(api_get_status /apis/apiextensions.k8s.io/v1/customresourcedefinitions/ironics.ironic.metal3.io)" = "200" ] &&
     python3 - "${split_dir}/get.json" <<'PY'
import json
import sys

document = json.loads(open(sys.argv[1]).read())
conditions = (document.get("status") or {}).get("conditions") or []
for condition in conditions:
    if condition.get("type") == "Established" and condition.get("status") == "True":
        raise SystemExit(0)
raise SystemExit(1)
PY
  then
    established=true
    break
  fi
  sleep 0.5
done
if [ "${established}" != true ]; then
  echo "The Ironic CRD did not become Established" >&2
  exit 1
fi

echo "  platform-rendered Ironic CR"
status=$(dry_run_status "${split_dir}/rendered-ironic-cr.json")
case "${status}" in
  200|201) echo "The API server accepts the platform-rendered Ironic CR (dry run)" ;;
  *)
    cat "${split_dir}/dry-run-response.json" >&2
    echo "The API server rejected the platform-rendered Ironic CR (HTTP ${status})" >&2
    exit 1
    ;;
esac

# Prove the gate is live: the same POST with a wrongly typed field must be
# rejected. Without this, a proxy or a skipped handler would look like success.
python3 - "${split_dir}/rendered-ironic-cr.json" "${split_dir}/broken-ironic-cr.json" <<'PY'
import json
import sys
from pathlib import Path

document = json.loads(Path(sys.argv[1]).read_text())
document["spec"]["version"] = 37.0
Path(sys.argv[2]).write_text(json.dumps(document))
PY
broken_status=$(dry_run_status "${split_dir}/broken-ironic-cr.json")
case "${broken_status}" in
  200|201)
    echo "The API server accepted a wrongly typed Ironic field; the dry-run gate is not active" >&2
    exit 1
    ;;
  *) echo "The dry-run gate rejects an invalid Ironic CR (HTTP ${broken_status})" ;;
esac

kill "${proxy_pid}" 2>/dev/null || true
wait "${proxy_pid}" 2>/dev/null || true
trap 'rm -rf "${split_dir}"' EXIT
