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
