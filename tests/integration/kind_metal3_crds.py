"""Staged integration check for the shipped Metal3 IPAM and SR-IOV CRDs.

Python replacement for the former ``tests/integration/kind_metal3_crds.sh``:
installs cert-manager, the IPAM and SR-IOV CRDs into a real kind API server,
then validates that the API server accepts every manifest this platform
renders, including a live admission dry-run of a platform-rendered Ironic CR
and a negative probe proving that gate is active.

Requires ``kubectl`` on PATH and PyYAML. Run inside a kind cluster:

    python3 tests/integration/kind_metal3_crds.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parents[2]
IPAM_MANIFEST = ROOT_DIR / "deploy/offline/addons/control-stack/ipam.yaml"
SRIOV_MANIFEST = ROOT_DIR / "deploy/offline/addons/sriov-network-operator/operator.yaml"
CONTROL_STACK_DIR = ROOT_DIR / "deploy/offline/addons/control-stack"

KUBECTL = os.environ.get("KUBECTL", "kubectl")
PROXY_PORT = int(os.environ.get("KIND_CRD_PROXY_PORT", "8010"))
PROXY_BASE = f"http://127.0.0.1:{PROXY_PORT}"

DOC_SPLIT = re.compile(r"(?m)^---[ \t]*$")


def run(cmd: list[str], *, stdin: str | None = None, capture: bool = False) -> str:
    result = subprocess.run(cmd, input=stdin, text=True, capture_output=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    return result.stdout if capture else ""


def kubectl(*args: str, stdin: str | None = None, capture: bool = False,
            check: bool = True) -> str:
    result = subprocess.run(
        [KUBECTL, *args], input=stdin, text=True, capture_output=True
    )
    if check and result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    return result.stdout if capture else ""


def apply_text(text: str, *, dry_run: bool = False) -> str:
    args = ["apply"]
    if dry_run:
        args.append("--dry-run=server")
    args.extend(["-f", "-"])
    return kubectl(*args, stdin=text, capture=True)


def expand_manifest(manifest: Path, destination: Path) -> None:
    sys.path.insert(0, str(ROOT_DIR / "packages"))
    from agent_contracts.kustomize_vars import expand_kustomize_vars

    destination.write_text(
        expand_kustomize_vars(manifest.read_text(encoding="utf-8")), encoding="utf-8"
    )


def load_module(name: str, path: Path):
    # The modules under apps/ and packages/ import each other by repository-
    # rooted names, so the repository root must be importable first.
    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def create_namespace(name: str) -> None:
    rendered = kubectl("create", "namespace", name, "--dry-run=client",
                       "-o", "yaml", capture=True)
    kubectl("apply", "-f", "-", stdin=rendered)


def render_network_objects(policy_path: Path, pool_path: Path) -> None:
    contract = load_module(
        "infra_networks_contract", ROOT_DIR / "packages/agent_contracts/infra_networks.py"
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
            },
        ]
    }
    plan = contract.plan_infra_networks(profile)
    policy_path.write_text(
        yaml.safe_dump_all(contract.render_sriov_node_policies(plan), sort_keys=False),
        encoding="utf-8",
    )
    pool_path.write_text(
        yaml.safe_dump_all(
            contract.render_ip_pools(plan, cluster_name="ci-cluster"), sort_keys=False
        ),
        encoding="utf-8",
    )


def render_ironic(payload_path: Path, crd_path: Path, cr_json: Path) -> None:
    documents = [
        document
        for document in yaml.safe_load_all(
            (CONTROL_STACK_DIR / "ironic.yaml").read_text(encoding="utf-8")
        )
        if document
    ]
    crds = [
        document
        for document in documents
        if document.get("kind") == "CustomResourceDefinition"
        and document["spec"]["names"]["kind"] == "Ironic"
    ]
    if not crds:
        raise SystemExit("the vendored control stack does not define the Ironic CRD")
    crd_path.write_text(yaml.safe_dump_all(crds, sort_keys=False), encoding="utf-8")

    ironic_cr = load_module(
        "ironic_cr_contract", ROOT_DIR / "apps/eph_agent/ironic_cr.py"
    )
    document = ironic_cr.render_ironic_cr(
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
    payload_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    cr_json.write_text(json.dumps(document), encoding="utf-8")


def resource_path(document: dict) -> str:
    version = document["apiVersion"]
    kind = document["kind"].lower()
    namespace = (document.get("metadata") or {}).get("namespace")
    group, _, api_version = version.partition("/")
    resource = kind + "s"
    if namespace:
        return f"/apis/{group}/{api_version}/namespaces/{namespace}/{resource}?dryRun=All"
    return f"/apis/{group}/{api_version}/{resource}?dryRun=All"


def http_post(path: str, body: Path) -> tuple[int, str]:
    request = urllib.request.Request(
        PROXY_BASE + path,
        data=body.read_bytes(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    except urllib.error.URLError as exc:
        raise SystemExit(f"kubectl proxy request failed: {exc}") from exc


def http_get(path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(PROXY_BASE + path, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    except urllib.error.URLError:
        return 0, ""


def wait_for_proxy(process: subprocess.Popen) -> None:
    for _ in range(80):
        if http_get("/version")[0] == 200:
            return
        time.sleep(0.25)
    process.terminate()
    raise SystemExit("kubectl proxy did not become ready")


def established(document: dict) -> bool:
    conditions = (document.get("status") or {}).get("conditions") or []
    return any(
        condition.get("type") == "Established" and condition.get("status") == "True"
        for condition in conditions
    )


def main() -> None:
    for manifest in (IPAM_MANIFEST, SRIOV_MANIFEST):
        if not manifest.is_file():
            raise SystemExit(f"Missing manifest: {manifest}")
    if shutil.which(KUBECTL) is None:
        raise SystemExit("kubectl is required")

    cert_manager_version = os.environ.get("CERT_MANAGER_VERSION", "v1.16.3")
    cert_manager_url = (
        "https://github.com/cert-manager/cert-manager/releases/download/"
        f"{cert_manager_version}/cert-manager.yaml"
    )

    with tempfile.TemporaryDirectory() as tmp:
        split_dir = Path(tmp)

        for namespace in ("capm3-system", "sriov-network-operator", "metal3"):
            create_namespace(namespace)

        print(f"Installing cert-manager {cert_manager_version} "
              "(IPAM webhook prerequisite)")
        kubectl("apply", "-f", cert_manager_url)
        kubectl(
            "wait", "--for", "condition=established", "--timeout=120s",
            "crd/certificates.cert-manager.io", "crd/issuers.cert-manager.io",
        )
        for deployment in ("cert-manager-webhook", "cert-manager-cainjector"):
            kubectl(
                "rollout", "status", f"deployment/{deployment}",
                "-n", "cert-manager", "--timeout=180s",
            )

        print("Applying Metal3 IPAM manifests (expanded)")
        ipam_expanded = split_dir / "ipam-expanded.yaml"
        expand_manifest(IPAM_MANIFEST, ipam_expanded)
        kubectl("apply", "-f", str(ipam_expanded))

        print("Waiting for the IPAM webhook to serve requests")
        kubectl(
            "wait", "--for", "condition=Ready", "--timeout=180s",
            "certificate/ipam-serving-cert", "-n", "metal3-ipam-system",
        )
        kubectl(
            "rollout", "status", "deployment/ipam-controller-manager",
            "-n", "metal3-ipam-system", "--timeout=240s",
        )

        print("Applying SR-IOV CRDs first, then the operator resources")
        sriov_documents = [
            document.strip()
            for document in DOC_SPLIT.split(
                SRIOV_MANIFEST.read_text(encoding="utf-8")
            )
            if document.strip()
        ]
        crds_text: list[str] = []
        resources_text: list[str] = []
        for document in sriov_documents:
            parsed = yaml.safe_load(document)
            if not parsed:
                continue
            (crds_text if parsed.get("kind") == "CustomResourceDefinition"
             else resources_text).append(document)
        crds_file = split_dir / "crds.yaml"
        resources_file = split_dir / "resources.yaml"
        crds_file.write_text("\n---\n".join(crds_text) + "\n", encoding="utf-8")
        resources_file.write_text(
            "\n---\n".join(resources_text) + "\n", encoding="utf-8"
        )
        kubectl("apply", "-f", str(crds_file))

        print("Waiting for required CRDs")
        for crd in (
            "ippools.ipam.metal3.io",
            "ipaddresses.ipam.metal3.io",
            "ipclaims.ipam.metal3.io",
            "sriovnetworknodepolicies.sriovnetwork.openshift.io",
        ):
            kubectl("wait", "--for", "condition=established", "--timeout=120s",
                   f"crd/{crd}")

        kubectl("apply", "-f", str(resources_file))

        print("Server-side validation of platform-rendered IPPool "
              "and SriovNetworkNodePolicy")
        rendered_policy = split_dir / "rendered-sriov-policy.yaml"
        rendered_ippool = split_dir / "rendered-ippool.yaml"
        render_network_objects(rendered_policy, rendered_ippool)
        kubectl("apply", "--dry-run=server", "-f", str(rendered_ippool))
        kubectl("apply", "--dry-run=server", "-f", str(rendered_policy))

        print("Metal3 and SR-IOV CRDs are present and accept rendered manifests")
        print("Server-side validation of every vendored control-stack manifest")
        for namespace in (
            "capm3-system",
            "capi-system",
            "capi-kubeadm-bootstrap-system",
            "capi-kubeadm-control-plane-system",
            "baremetal-operator-system",
            "ironic-standalone-operator-system",
        ):
            create_namespace(namespace)
        expanded = split_dir / "expanded.yaml"
        for manifest in sorted(CONTROL_STACK_DIR.glob("*.yaml")):
            print(f"  {manifest.name}")
            expand_manifest(manifest, expanded)
            kubectl("apply", "--dry-run=server", "-f", str(expanded))
        print("All control-stack manifests are accepted by the API server")

        print("CRD-schema validation of the platform-rendered Ironic CR")
        ironic_crd = split_dir / "ironic-crd.yaml"
        rendered_ironic = split_dir / "rendered-ironic-cr.yaml"
        ironic_json = split_dir / "rendered-ironic-cr.json"
        render_ironic(rendered_ironic, ironic_crd, ironic_json)
        crd_json = split_dir / "ironic-crd.json"
        crd_json.write_text(
            json.dumps(list(yaml.safe_load_all(ironic_crd.read_text()))[0]),
            encoding="utf-8",
        )
        namespace_json = split_dir / "namespace.json"
        namespace_json.write_text(
            json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": "baremetal-operator-system"},
                }
            ),
            encoding="utf-8",
        )

        proxy = subprocess.Popen(
            [KUBECTL, "proxy", f"--port={PROXY_PORT}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wait_for_proxy(proxy)

            print("  namespace baremetal-operator-system")
            namespace_status, body = http_post("/api/v1/namespaces", namespace_json)
            if namespace_status not in (200, 201, 409):
                sys.stderr.write(body)
                raise SystemExit(
                    "The API server rejected the workspace namespace "
                    f"(HTTP {namespace_status})"
                )

            print("  Ironic CRD")
            crd_status, body = http_post(
                "/apis/apiextensions.k8s.io/v1/customresourcedefinitions", crd_json
            )
            if crd_status not in (200, 201, 409):
                sys.stderr.write(body)
                raise SystemExit(
                    f"The API server rejected the Ironic CRD (HTTP {crd_status})"
                )

            is_established = False
            for _ in range(120):
                status, body = http_get(
                    "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/"
                    "ironics.ironic.metal3.io"
                )
                if status == 200 and established(json.loads(body)):
                    is_established = True
                    break
                time.sleep(0.5)
            if not is_established:
                raise SystemExit("The Ironic CRD did not become Established")

            print("  platform-rendered Ironic CR")
            status, body = http_post(resource_path(json.loads(ironic_json.read_text())),
                                     ironic_json)
            if status in (200, 201):
                print("The API server accepts the platform-rendered Ironic CR (dry run)")
            else:
                sys.stderr.write(body)
                raise SystemExit(
                    "The API server rejected the platform-rendered Ironic CR "
                    f"(HTTP {status})"
                )

            broken = split_dir / "broken-ironic-cr.json"
            document = json.loads(ironic_json.read_text())
            document["spec"]["version"] = 37.0
            broken.write_text(json.dumps(document), encoding="utf-8")
            status, body = http_post(resource_path(document), broken)
            if status in (200, 201):
                raise SystemExit(
                    "The API server accepted a wrongly typed Ironic field; "
                    "the dry-run gate is not active"
                )
            print(f"The dry-run gate rejects an invalid Ironic CR (HTTP {status})")
        finally:
            proxy.terminate()
            try:
                proxy.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proxy.kill()


if __name__ == "__main__":
    main()
