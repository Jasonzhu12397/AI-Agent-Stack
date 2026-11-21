"""Read-only acceptance checks for a real Metal3/Ironic deployment.

This tool never mutates a BMC, a BMH, Ironic or Kubernetes. It collects
evidence, compares it with the requested topology and exits non-zero when a
check fails. Unit tests mock the transport; passing tests do not prove that any
physical machine was provisioned.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "apps" / "platform-api"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from tools.metal3_deploy import local_command_runner  # noqa: E402
from tools.metal3_fleet_source import PlatformMachine, load_platform_fleet  # noqa: E402

DEFAULT_SYSTEMS_PATH = "/redfish/v1/Systems/1"
ACCEPTANCE_VERSION = "1"
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_INFO = "info"


class AcceptanceError(ValueError):
    pass


def _check(name: str, status: str, detail: str, **facts: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, **facts}


def _redfish_origin(address: str) -> str:
    parsed = urllib.parse.urlsplit(address)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AcceptanceError(f"invalid BMC address: {address}")
    return f"{parsed.scheme}://{parsed.netloc}"


def _redfish_get(
    machine: PlatformMachine,
    path: str,
    *,
    timeout: int = 10,
) -> dict[str, Any]:
    url = _redfish_origin(machine.spec.bmc_address) + path
    request = urllib.request.Request(url, method="GET")
    token = base64.b64encode(
        f"{machine.username}:{machine.password}".encode()
    ).decode("ascii")
    request.add_header("Authorization", f"Basic {token}")
    request.add_header("Accept", "application/json")
    context = None
    if url.startswith("https://"):
        context = (
            ssl.create_default_context()
            if machine.spec.tls_verify
            else ssl._create_unverified_context()  # noqa: S323 - explicit BMC opt-out
        )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            payload = response.read(2 * 1024 * 1024)
            return {
                "status": response.status,
                "json": json.loads(payload.decode("utf-8")),
            }
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "json": None, "error": "HTTPError"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"status": 0, "json": None, "error": type(exc).__name__}
    except json.JSONDecodeError:
        return {"status": 200, "json": None, "error": "INVALID_JSON"}


def _macs_from_interfaces(payload: dict[str, Any]) -> list[str]:
    macs = []
    for item in payload.get("Members", []):
        if not isinstance(item, dict):
            continue
        value = item.get("MACAddress")
        if isinstance(value, str) and value:
            macs.append(value.lower())
    return macs


def probe_redfish(machine: PlatformMachine, *, timeout: int = 10) -> dict[str, Any]:
    """Read the BMC system and EthernetInterfaces collections without mutation."""

    systems = _redfish_get(machine, DEFAULT_SYSTEMS_PATH, timeout=timeout)
    checks = []
    if systems["status"] != 200 or not isinstance(systems["json"], dict):
        checks.append(
            _check(
                "redfish.reachable",
                STATUS_FAIL,
                f"GET {DEFAULT_SYSTEMS_PATH} returned status {systems['status']} "
                f"({systems.get('error', 'no body')}).",
            )
        )
        return {"machine": machine.spec.name, "checks": checks, "system": None}
    system = systems["json"]
    checks.append(_check("redfish.reachable", STATUS_PASS, "Systems endpoint returned 200."))
    power_state = system.get("PowerState")
    checks.append(
        _check(
            "redfish.power_state",
            STATUS_INFO,
            f"PowerState={power_state}",
            power_state=power_state,
        )
    )
    boot = system.get("Boot") if isinstance(system.get("Boot"), dict) else {}
    checks.append(
        _check(
            "redfish.boot_override",
            STATUS_INFO,
            (
                f"target={boot.get('BootSourceOverrideTarget')} "
                f"enabled={boot.get('BootSourceOverrideEnabled')}"
            ),
            target=boot.get("BootSourceOverrideTarget"),
            enabled=boot.get("BootSourceOverrideEnabled"),
        )
    )
    interfaces_link = None
    links = system.get("Links") if isinstance(system.get("Links"), dict) else {}
    collection = system.get("EthernetInterfaces")
    if isinstance(collection, dict):
        interfaces_link = collection.get("@odata/id") or collection.get("@odata.id")
    if not interfaces_link and isinstance(links.get("EthernetInterfaces"), dict):
        interfaces_link = links["EthernetInterfaces"].get("@odata.id")
    interfaces_link = interfaces_link or f"{DEFAULT_SYSTEMS_PATH}/EthernetInterfaces"
    interfaces = _redfish_get(machine, str(interfaces_link), timeout=timeout)
    macs = _macs_from_interfaces(interfaces["json"] or {})
    expected = machine.spec.boot_mac.lower()
    if macs and expected in macs:
        checks.append(
            _check("redfish.boot_mac", STATUS_PASS, "Boot MAC is present on the BMC.", macs=macs)
        )
    elif macs:
        checks.append(
            _check(
                "redfish.boot_mac",
                STATUS_FAIL,
                "Boot MAC is absent from the BMC EthernetInterfaces collection.",
                expected=expected,
                macs=macs,
            )
        )
    else:
        checks.append(
            _check(
                "redfish.boot_mac",
                STATUS_FAIL,
                f"Could not read EthernetInterfaces (status {interfaces['status']}).",
                expected=expected,
            )
        )
    return {"machine": machine.spec.name, "checks": checks, "system": system}


def _kubectl_json(
    arguments: list[str],
    *,
    kubeconfig: Path | None,
    timeout: int = 60,
) -> dict[str, Any]:
    argv = ["kubectl", *(["--kubeconfig", str(kubeconfig)] if kubeconfig else []), *arguments]
    result = local_command_runner(argv, timeout=timeout)
    if not result.get("available"):
        return {"ok": False, "code": "KUBECTL_UNAVAILABLE", "payload": None}
    if result.get("returncode") != 0:
        return {
            "ok": False,
            "code": "KUBECTL_FAILED",
            "stderr": str(result.get("stderr", ""))[-1200:],
            "payload": None,
        }
    try:
        payload = json.loads(result.get("stdout", "") or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "code": "KUBECTL_INVALID_JSON", "payload": None}
    return {"ok": True, "payload": payload}


def _items(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def cluster_snapshot(
    *,
    kubeconfig: Path | None,
    target_kubeconfig: Path | None = None,
) -> dict[str, Any]:
    resources = {
        "baremetalhosts": ["get", "baremetalhosts.metal3.io", "-A", "-o", "json"],
        "clusters": ["get", "clusters.cluster.x-k8s.io", "-A", "-o", "json"],
        "machines": ["get", "machines.cluster.x-k8s.io", "-A", "-o", "json"],
        "control_planes": [
            "get",
            "kubeadmcontrolplanes.controlplane.cluster.x-k8s.io",
            "-A",
            "-o",
            "json",
        ],
        "machine_deployments": ["get", "machinedeployments.cluster.x-k8s.io", "-A", "-o", "json"],
    }
    snapshot: dict[str, Any] = {}
    for key, arguments in resources.items():
        snapshot[key] = _kubectl_json(arguments, kubeconfig=kubeconfig)
    if target_kubeconfig is not None:
        snapshot["nodes"] = _kubectl_json(
            ["get", "nodes", "-o", "json"],
            kubeconfig=target_kubeconfig,
        )
        snapshot["calico"] = _kubectl_json(
            ["-n", "kube-system", "get", "daemonset", "calico-node", "-o", "json"],
            kubeconfig=target_kubeconfig,
        )
        snapshot["kube_proxy"] = _kubectl_json(
            ["-n", "kube-system", "get", "daemonset", "kube-proxy", "-o", "json"],
            kubeconfig=target_kubeconfig,
        )
    return snapshot


def structural_checks(machines: list[PlatformMachine], skipped: list[str]) -> dict[str, Any]:
    checks = []
    for message in skipped:
        checks.append(_check("fleet.planning_input", STATUS_FAIL, message))
    for machine in machines:
        spec = machine.spec
        flags = []
        if spec.cpu_cores < 4:
            flags.append(f"cpu_cores={spec.cpu_cores}")
        if spec.ram_mib < 8192:
            flags.append(f"ram_mib={spec.ram_mib}")
        if len(spec.boot_mac.split(":")) != 6:
            flags.append(f"boot_mac={spec.boot_mac}")
        if flags:
            checks.append(
                _check(
                    "machine.structure",
                    STATUS_FAIL,
                    f"{spec.name}: " + ", ".join(flags),
                    machine=spec.name,
                )
            )
        else:
            checks.append(
                _check(
                    "machine.structure",
                    STATUS_PASS,
                    (
                        f"{spec.name}: cpu={spec.cpu_cores} ram={spec.ram_mib}MiB "
                        f"disk={spec.disk_gib}GiB mac={spec.boot_mac}"
                    ),
                    machine=spec.name,
                )
            )
    return {"checks": checks}


def _bmh_status(bmh: dict[str, Any]) -> tuple[str, str]:
    status = bmh.get("status", {}) if isinstance(bmh.get("status"), dict) else {}
    operational = status.get("operationalStatus", "unknown")
    provisioning = (
        status.get("provisioning", {})
        if isinstance(status.get("provisioning"), dict)
        else {}
    )
    state = provisioning.get("state", "unknown")
    return str(operational), str(state)


def verify_cluster(
    snapshot: dict[str, Any],
    *,
    expected_machines: list[str],
    expected_control_plane: int,
    expected_workers: int,
) -> dict[str, Any]:
    checks = []
    for key, label in (
        ("baremetalhosts", "cluster.bmh_readable"),
        ("machines", "cluster.machines_readable"),
        ("control_planes", "cluster.control_planes_readable"),
        ("machine_deployments", "cluster.machine_deployments_readable"),
    ):
        result = snapshot.get(key, {})
        if not result.get("ok"):
            checks.append(
                _check(label, STATUS_FAIL, f"{result.get('code')}: {result.get('stderr', '')}")
            )
    bmhs = _items(snapshot.get("baremetalhosts", {}).get("payload"))
    bmh_names = {
        str(item.get("metadata", {}).get("name", "")) for item in bmhs
    }
    if expected_machines:
        missing = sorted(name for name in expected_machines if name not in bmh_names)
        if missing:
            checks.append(
                _check(
                    "cluster.bmh_inventory",
                    STATUS_FAIL,
                    "Missing BMHs: " + ", ".join(missing),
                )
            )
        else:
            checks.append(
                _check(
                    "cluster.bmh_inventory",
                    STATUS_PASS,
                    f"{len(expected_machines)} BMHs present.",
                )
            )
    else:
        checks.append(
            _check(
                "cluster.bmh_inventory",
                STATUS_INFO,
                f"{len(bmhs)} BMHs discovered; no expected inventory supplied.",
            )
        )
    for bmh in bmhs:
        name = str(bmh.get("metadata", {}).get("name", ""))
        operational, state = _bmh_status(bmh)
        ok = operational == "OK" and state in {"provisioned", "externallyProvisioned"}
        checks.append(
            _check(
                "cluster.bmh_provisioned",
                STATUS_PASS if ok else STATUS_FAIL,
                f"{name}: operational={operational} provisioning={state}",
                machine=name,
                operational=operational,
                provisioning=state,
            )
        )
    control_planes = _items(snapshot.get("control_planes", {}).get("payload"))
    ready_control_plane = sum(
        int(item.get("status", {}).get("readyReplicas", 0) or 0)
        for item in control_planes
    )
    checks.append(
        _check(
            "cluster.control_plane_replicas",
            STATUS_PASS if ready_control_plane == expected_control_plane else STATUS_FAIL,
            f"readyReplicas={ready_control_plane}, expected={expected_control_plane}",
        )
    )
    deployments = _items(snapshot.get("machine_deployments", {}).get("payload"))
    ready_workers = sum(
        int(item.get("status", {}).get("readyReplicas", 0) or 0)
        for item in deployments
    )
    checks.append(
        _check(
            "cluster.worker_replicas",
            STATUS_PASS if ready_workers == expected_workers else STATUS_FAIL,
            f"readyReplicas={ready_workers}, expected={expected_workers}",
        )
    )
    if "nodes" in snapshot:
        nodes_result = snapshot.get("nodes", {})
        if not nodes_result.get("ok"):
            checks.append(
                _check("target.nodes_readable", STATUS_FAIL, str(nodes_result.get("code")))
            )
        else:
            nodes = _items(nodes_result.get("payload"))
            ready = [
                node
                for node in nodes
                if all(
                    condition.get("type") == "Ready" and condition.get("status") == "True"
                    for condition in node.get("status", {}).get("conditions", [])
                )
            ]
            checks.append(
                _check(
                    "target.nodes_ready",
                    STATUS_PASS if len(ready) == len(nodes) and nodes else STATUS_FAIL,
                    f"{len(ready)}/{len(nodes)} nodes Ready.",
                )
            )
        calico = snapshot.get("calico", {})
        if not calico.get("ok"):
            checks.append(
                _check("target.calico_present", STATUS_FAIL, str(calico.get("code")))
            )
        else:
            daemonsets = _items(calico.get("payload"))
            bpf = False
            for daemonset in daemonsets:
                pod_spec = (
                    daemonset.get("spec", {}).get("template", {}).get("spec", {})
                )
                containers = pod_spec.get("containers", [])
                for container in containers:
                    for env in container.get("env", []):
                        if (
                            env.get("name") == "FELIX_BPFENABLED"
                            and str(env.get("value")).lower() == "true"
                        ):
                            bpf = True
            checks.append(
                _check(
                    "target.calico_ebpf",
                    STATUS_PASS if bpf else STATUS_FAIL,
                    "FELIX_BPFENABLED=true" if bpf else "FELIX_BPFENABLED was not true.",
                )
            )
        proxy = snapshot.get("kube_proxy", {})
        proxy_items = _items(proxy.get("payload")) if proxy.get("ok") else []
        checks.append(
            _check(
                "target.kube_proxy_absent",
                STATUS_PASS if proxy.get("ok") and not proxy_items else STATUS_FAIL,
                (
                    "kube-proxy DaemonSet absent."
                    if proxy.get("ok") and not proxy_items
                    else "kube-proxy is still present."
                ),
            )
        )
    return {"checks": checks}


def overall_status(checks: list[dict[str, Any]]) -> str:
    return STATUS_FAIL if any(item["status"] == STATUS_FAIL for item in checks) else STATUS_PASS


def write_evidence(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _kubectl_runner(kubeconfig: str):
    """A runner that shells out to kubectl against the target cluster."""

    def run(argv: list[str], timeout: int = 20, **_kwargs: Any) -> dict[str, Any]:
        env = {**os.environ, "KUBECONFIG": kubeconfig}
        try:
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "available": False,
                "returncode": 1,
                "stdout": "",
                "stderr": str(exc),
            }
        return {
            "available": True,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr[:2000],
        }

    return run


def command_rollback(args) -> int:
    """Read-only: prove this lab cluster could be rolled back at all."""

    from target_agent.kubernetes_rollback_exec import (
        RollbackExecutionError,
        RollbackExecutor,
    )

    request = {
        "cluster_name": args.cluster_name or "lab",
        "snapshot_path": args.snapshot,
        "execution_mode": "simulation",
        "confirm_cluster_name": "",
    }
    try:
        assessed = RollbackExecutor(_kubectl_runner(args.kubeconfig)).assess(request)
    except RollbackExecutionError as exc:
        print(json.dumps({"status": STATUS_FAIL, "code": exc.code, "detail": exc.detail}, indent=2))
        return 1
    checks = [
        _check(
            item["name"],
            STATUS_PASS if item["ok"] else STATUS_FAIL,
            item["detail"],
        )
        for item in assessed["checks"]
    ]
    payload = {
        "kind": "metal3-rollback-readiness",
        "version": ACCEPTANCE_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "snapshot": args.snapshot,
        "control_planes": assessed["control_planes"],
        "control_plane": assessed["control_plane"],
        "etcd_pod": assessed["etcd_pod"],
        "refusal": assessed["refusal"],
        "checks": checks,
        "status": overall_status(checks),
    }
    if args.out:
        write_evidence(Path(args.out), payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == STATUS_PASS else 1


def command_preflight(args) -> int:
    skipped: list[str] = []
    machines = load_platform_fleet(
        project_id=args.project,
        settings_module=args.django_settings,
        systems_path=args.redfish_systems_path,
        skipped=skipped,
        require_credentials=False,
    )
    payload = structural_checks(machines, skipped)
    payload.update(
        {
            "kind": "metal3-preflight",
            "version": ACCEPTANCE_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "project": args.project,
            "machine_count": len(machines),
        }
    )
    payload["status"] = overall_status(payload["checks"])
    if args.out:
        write_evidence(Path(args.out), payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == STATUS_PASS else 1


def command_redfish(args) -> int:
    machines = load_platform_fleet(
        project_id=args.project,
        settings_module=args.django_settings,
        systems_path=args.redfish_systems_path,
        require_credentials=True,
    )
    results = [probe_redfish(machine, timeout=args.timeout) for machine in machines]
    checks = [item for result in results for item in result["checks"]]
    payload = {
        "kind": "metal3-redfish-evidence",
        "version": ACCEPTANCE_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "project": args.project,
        "machines": [result["machine"] for result in results],
        "checks": checks,
        "status": overall_status(checks),
    }
    if args.out:
        write_evidence(Path(args.out), payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == STATUS_PASS else 1


def command_cluster(args) -> int:
    snapshot = cluster_snapshot(
        kubeconfig=Path(args.kubeconfig) if args.kubeconfig else None,
        target_kubeconfig=Path(args.target_kubeconfig) if args.target_kubeconfig else None,
    )
    checks = verify_cluster(
        snapshot,
        expected_machines=[],
        expected_control_plane=args.control_plane_replicas,
        expected_workers=args.worker_replicas,
    )
    payload = {
        "kind": "metal3-cluster-evidence",
        "version": ACCEPTANCE_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "checks": checks,
        "status": overall_status(checks),
    }
    if args.out:
        write_evidence(Path(args.out), payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == STATUS_PASS else 1


def command_verify(args) -> int:
    skipped: list[str] = []
    machines = load_platform_fleet(
        project_id=args.project,
        settings_module=args.django_settings,
        systems_path=args.redfish_systems_path,
        skipped=skipped,
        require_credentials=False,
    )
    checks = structural_checks(machines, skipped)["checks"]
    snapshot = cluster_snapshot(
        kubeconfig=Path(args.kubeconfig) if args.kubeconfig else None,
        target_kubeconfig=Path(args.target_kubeconfig) if args.target_kubeconfig else None,
    )
    checks.extend(
        verify_cluster(
            snapshot,
            expected_machines=[machine.spec.name for machine in machines],
            expected_control_plane=args.control_plane_replicas,
            expected_workers=args.worker_replicas,
        )["checks"]
    )
    if args.redfish:
        for machine in load_platform_fleet(
            project_id=args.project,
            settings_module=args.django_settings,
            systems_path=args.redfish_systems_path,
            require_credentials=True,
        ):
            checks.extend(probe_redfish(machine, timeout=args.timeout)["checks"])
    payload = {
        "kind": "metal3-acceptance",
        "version": ACCEPTANCE_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "project": args.project,
        "machine_count": len(machines),
        "checks": checks,
        "status": overall_status(checks),
    }
    if args.out:
        write_evidence(Path(args.out), payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == STATUS_PASS else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--django-settings", default="platform_api.settings")
    parser.add_argument("--redfish-systems-path", default=DEFAULT_SYSTEMS_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight", help="Check fleet planning input only.")
    preflight.add_argument("--project", required=True)
    preflight.add_argument("--out")
    preflight.set_defaults(func=command_preflight)

    redfish = subparsers.add_parser("redfish", help="Read-only BMC Redfish evidence.")
    redfish.add_argument("--project", required=True)
    redfish.add_argument("--timeout", type=int, default=10)
    redfish.add_argument("--out")
    redfish.set_defaults(func=command_redfish)

    cluster = subparsers.add_parser("cluster", help="Read-only CAPI/Metal3 state.")
    cluster.add_argument("--kubeconfig", required=True)
    cluster.add_argument("--target-kubeconfig")
    cluster.add_argument("--control-plane-replicas", type=int, default=3)
    cluster.add_argument("--worker-replicas", type=int, default=0)
    cluster.add_argument("--out")
    cluster.set_defaults(func=command_cluster)

    verify = subparsers.add_parser(
        "verify",
        help="Combine structural, CAPI and optional BMC checks.",
    )
    verify.add_argument("--project", required=True)
    verify.add_argument("--kubeconfig", required=True)
    verify.add_argument("--target-kubeconfig")
    verify.add_argument("--control-plane-replicas", type=int, default=3)
    verify.add_argument("--worker-replicas", type=int, default=0)
    verify.add_argument("--redfish", action="store_true")
    verify.add_argument("--timeout", type=int, default=10)
    verify.add_argument("--out")
    verify.set_defaults(func=command_verify)

    rollback = subparsers.add_parser(
        "rollback",
        help="Read-only: can this cluster be rolled back from a snapshot?",
    )
    rollback.add_argument("--kubeconfig", required=True)
    rollback.add_argument(
        "--snapshot",
        required=True,
        help="pre-upgrade .db path as the etcd pod sees it",
    )
    rollback.add_argument("--cluster-name", default="")
    rollback.add_argument("--out")
    rollback.set_defaults(func=command_rollback)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "timeout", 10) < 1 or getattr(args, "timeout", 10) > 120:
        parser.error("--timeout must be between 1 and 120 seconds")
    try:
        return args.func(args)
    except AcceptanceError as exc:
        print(json.dumps({"status": STATUS_FAIL, "detail": str(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
