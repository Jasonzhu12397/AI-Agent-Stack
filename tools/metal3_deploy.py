#!/usr/bin/env python3
"""Plan, render and deploy a Metal3 target cluster without shell parsing.

This is the single entry point the lab scripts call. It never parses cluster
data in bash: the fleet comes from the platform database, the role decision is
reasoned in :mod:`apps.eph_agent.fleet_planning`, the manifests are rendered by
:mod:`apps.eph_agent.capi_renderer` and the apply/verify steps are the agent's
own skills in :mod:`apps.eph_agent.metal3_skill`.

Commands:

* ``plan``    - read the fleet, reason about roles, write ``plan.json``,
  ``secrets.yaml``, ``bmh.yaml`` and, when the cluster inputs are supplied,
  ``capi.yaml``.
* ``apply``   - hand the rendered bundle to the agent: Secrets, then BMHs, then
  the CAPI/Metal3 cluster resources.
* ``verify``  - ask the agent to judge the BMHs against the planned host count
  and judge the management cluster (CRDs, controllers, host bridges, infra
  containers, Ironic authentication) in python, never by grepping kubectl.
* ``explain`` - print the recorded reasoning, one line per machine.

The sequence this supports is the one the platform uses: the EPH live-OS single
node cluster carries Cluster API plus Metal3, that renders and deploys the
target cluster's control plane, the target agent then pivots and provisions the
worker (data plane) machines, and the network stack adds the CNI and the data
plane plugins.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "apps" / "platform-api"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from apps.eph_agent import (  # noqa: E402
    capi_renderer,
    data_template,
    fleet_planning,
    metal3_readiness,
    metal3_skill,
)
from apps.target_agent import network_plugins, network_stack_adapter  # noqa: E402
from tools.metal3_fleet_source import load_platform_fleet  # noqa: E402

MAX_OUTPUT_BYTES = 4 * 1024 * 1024
PLAN_FILE = "plan.json"
SECRETS_FILE = "secrets.yaml"
BMH_FILE = "bmh.yaml"
CAPI_FILE = "capi.yaml"
NETWORK_SPEC_FILE = "network-spec.yaml"
NETWORK_FILE = "network.json"
TARGET_KUBECONFIG_FILE = "target.kubeconfig"
READY_STATES = {"available", "ready", "provisioned"}


# --------------------------------------------------------------------------- runner


class KubectlRunner:
    """Runner for the agent skills; manifests go in through stdin."""

    def __init__(
        self,
        documents: list[dict[str, Any]] | None = None,
        kubeconfig: Path | None = None,
    ) -> None:
        self.documents = documents
        self.kubeconfig = kubeconfig

    def __call__(self, arguments: list[str], timeout: int = 60, **_: Any) -> dict[str, Any]:
        payload = None
        if self.documents is not None:
            payload = yaml.safe_dump_all(self.documents, sort_keys=False)
        try:
            completed = subprocess.run(
                [
                    "kubectl",
                    *(["--kubeconfig", str(self.kubeconfig)] if self.kubeconfig else []),
                    *arguments,
                ],
                check=False,
                capture_output=True,
                text=True,
                input=payload,
                timeout=timeout,
                env={**os.environ},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"available": False, "error": type(exc).__name__}
        return {
            "available": True,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-MAX_OUTPUT_BYTES:],
            "stderr": completed.stderr[-4000:],
        }


def _plain_runner(kubeconfig: Path | None = None) -> KubectlRunner:
    return KubectlRunner(None, kubeconfig=kubeconfig)


def local_command_runner(arguments: list[str], timeout: int = 30, **_: Any) -> dict[str, Any]:
    """Run a host network command and return its raw result."""

    try:
        completed = subprocess.run(
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "error": type(exc).__name__}
    return {
        "available": True,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-MAX_OUTPUT_BYTES:],
        "stderr": completed.stderr[-4000:],
    }


def http_status_probe(url: str) -> int | None:
    """Return the HTTP status of an endpoint, or None when it cannot be reached.

    Ironic serves a self-signed certificate in this lab, so verification is
    deliberately off for this probe only: the question is whether the API
    demands authentication, not whether its certificate is trusted.
    """

    import ssl
    import urllib.error
    import urllib.request

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=15, context=context) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (OSError, ValueError):
        return None


# ----------------------------------------------------------------------------- plan


def _secret_document(machine) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": machine.spec.secret_ref},
        "type": "Opaque",
        "stringData": {"username": machine.username, "password": machine.password},
    }


def command_plan(args: argparse.Namespace) -> int:
    skipped: list[str] = []
    fleet = load_platform_fleet(
        project_id=args.project,
        settings_module=args.django_settings,
        systems_path=args.redfish_systems_path,
        skipped=skipped,
    )
    for item in skipped:
        sys.stderr.write(f"WARNING: skipped {item}\n")
    if not fleet:
        sys.stderr.write("ERROR: the platform has no machine ready for planning\n")
        return 1

    plan = fleet_planning.plan_fleet(
        [machine.spec for machine in fleet],
        cluster_name=args.cluster_name,
        namespace=args.namespace,
        control_plane_count=args.control_plane_count,
    )
    for warning in plan.warnings:
        sys.stderr.write(f"WARNING: {warning}\n")

    hosts = fleet_planning.bmh_host_plan(plan)
    bmh_documents = capi_renderer.render_bmh_manifests(hosts, namespace=args.namespace)

    credentials = {machine.spec.name: machine for machine in fleet}
    secrets = [
        _secret_document(credentials[assignment.machine.name]) for assignment in plan.assignments
    ]

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / PLAN_FILE).write_text(
        json.dumps(fleet_planning.plan_document(plan), indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / SECRETS_FILE).write_text(
        yaml.safe_dump_all(secrets, sort_keys=False), encoding="utf-8"
    )
    (out_dir / BMH_FILE).write_text(
        yaml.safe_dump_all(bmh_documents, sort_keys=False), encoding="utf-8"
    )

    capi_documents: list[dict[str, Any]] = []
    if args.control_plane_endpoint and args.image_url:
        network_spec = None
        if args.network_spec:
            network_spec = yaml.safe_load(args.network_spec.read_text(encoding="utf-8"))
            (out_dir / NETWORK_SPEC_FILE).write_text(
                yaml.safe_dump(network_spec, sort_keys=False), encoding="utf-8"
            )
        template_content = None
        if args.data_template:
            template_content = yaml.safe_load(args.data_template.read_text(encoding="utf-8"))
        else:
            template_content = data_template.default_data_template(network_spec)
        spec = fleet_planning.cluster_spec(
            plan,
            control_plane_endpoint=args.control_plane_endpoint,
            image_url=args.image_url,
            k8s_version=args.k8s_version,
            data_template=template_content,
            worker_node_labels=_parse_pool_labels(args.worker_node_labels),
            registry_endpoint=(network_spec or {}).get("registry_endpoint"),
        )
        capi_documents = capi_renderer.render_capi_metal3_manifests(spec)
        (out_dir / CAPI_FILE).write_text(
            yaml.safe_dump_all(capi_documents, sort_keys=False), encoding="utf-8"
        )
    elif args.control_plane_endpoint or args.image_url:
        sys.stderr.write(
            "WARNING: --control-plane-endpoint and --image-url must be given together "
            "to render the CAPI manifests; skipping capi.yaml\n"
        )

    if args.write_back:
        from tools.metal3_writeback import write_back_plan

        result = write_back_plan(
            plan,
            settings_module=args.django_settings,
            extra_metadata={"planned_by": "tools/metal3_deploy.py"},
        )
        print(
            f"written back to the platform: {result['updated']} machine record(s) updated, "
            f"{result['audited']} audit event(s)"
        )

    print(
        f"planned {len(plan.assignments)} machine(s): "
        f"{len(plan.by_role(fleet_planning.ROLE_CONTROL_PLANE))} control plane, "
        f"{len(plan.by_role(fleet_planning.ROLE_WORKER))} worker"
    )
    for line in plan.rationale():
        print(f"  {line}")
    print(f"plan: {out_dir / PLAN_FILE}")
    print(f"secrets: {out_dir / SECRETS_FILE}")
    print(f"bmh: {out_dir / BMH_FILE}")
    if capi_documents:
        print(f"capi: {out_dir / CAPI_FILE}")
    return 0


def _parse_pool_labels(value: str | None) -> dict[str, list[str]]:
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise SystemExit("ERROR: --worker-node-labels must be a JSON object of pool -> labels")
    return {str(pool): [str(label) for label in labels] for pool, labels in parsed.items()}


# ---------------------------------------------------------------------------- apply


def _load(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise SystemExit(f"ERROR: manifest not found: {path}")
    return [item for item in yaml.safe_load_all(path.read_text(encoding="utf-8")) if item]


def command_apply(args: argparse.Namespace) -> int:
    namespace = args.namespace

    secrets = _load(args.dir / SECRETS_FILE)
    for secret in secrets:
        name = secret.get("metadata", {}).get("name")
        runner = KubectlRunner([secret])
        result = runner(["apply", "--server-side", "-f", "-", "--namespace", namespace], 60)
        if result.get("available") is not True or result.get("returncode") != 0:
            sys.stderr.write(
                f"ERROR: failed to apply BMC credential Secret {name}: {result.get('stderr', '')}\n"
            )
            return 1

    hosts = _load(args.dir / BMH_FILE)
    applied = metal3_skill.apply_baremetalhosts(
        {"namespace": namespace, "manifests": hosts},
        KubectlRunner(hosts),
        None,
    )
    print(f"agent applied {len(hosts)} BareMetalHost object(s) in {namespace}")

    capi_path = args.dir / CAPI_FILE
    if capi_path.is_file():
        documents = _load(capi_path)
        metal3_skill.apply_capi_cluster_resources(
            {"namespace": namespace, "manifests": documents},
            KubectlRunner(documents),
            None,
        )
        print(f"agent applied {len(documents)} Cluster API object(s) in {namespace}")

    del applied
    return 0


# --------------------------------------------------------------------------- verify


def command_verify(args: argparse.Namespace) -> int:
    observation = metal3_skill.observe_baremetalhosts(
        {"namespace": args.namespace},
        _plain_runner(),
    )
    failures: list[str] = []
    expected = args.expected
    if expected is None:
        plan_path = args.dir / PLAN_FILE
        if plan_path.is_file():
            expected = len(json.loads(plan_path.read_text(encoding="utf-8")).get("assignments", []))
    if expected is not None and observation.get("host_count") != expected:
        failures.append(
            f"expected {expected} BareMetalHost(s), the agent sees {observation.get('host_count')}"
        )

    for host in observation.get("hosts", []):
        name = host.get("name")
        state = host.get("provisioning_state")
        status = host.get("operational_status")
        error = host.get("error_message") or ""
        ok = status == "OK" and state in READY_STATES and not error
        print(
            f"{'OK  ' if ok else 'FAIL'} {name}: operationalStatus={status} provisioning={state}"
            + (f" error={error[:120]}" if error else "")
        )
        if not ok:
            failures.append(f"{name} is not ready (status={status}, state={state})")

    if not args.bmh_only:
        findings = metal3_readiness.verify_management_cluster(
            _plain_runner(),
            kubeconfig=args.kubeconfig,
            host_runner=None if args.no_host_checks else local_command_runner,
            http_probe=None if args.no_host_checks else http_status_probe,
            ironic_url=args.ironic_url,
        )
        for finding in findings:
            print(finding.render())
            if not finding.ok:
                failures.append(finding.name)

    for failure in failures:
        sys.stderr.write(f"ERROR: {failure}\n")
    return 1 if failures else 0


# -------------------------------------------------------------------------- explain


def command_explain(args: argparse.Namespace) -> int:
    plan_path = args.dir / PLAN_FILE
    if not plan_path.is_file():
        sys.stderr.write(f"ERROR: no plan recorded in {args.dir}\n")
        return 1
    document = json.loads(plan_path.read_text(encoding="utf-8"))
    print(
        f"cluster {document['cluster_name']} in {document['namespace']}: "
        f"control plane of {document['control_plane_count']}"
    )
    for warning in document.get("warnings", []):
        print(f"  warning: {warning}")
    for assignment in document.get("assignments", []):
        gaps = assignment.get("missing_facts") or []
        suffix = f" [missing: {', '.join(gaps)}]" if gaps else ""
        print(
            f"  {assignment['role']:<13} {assignment['machine']:<20} {assignment['reason']}{suffix}"
        )
    return 0


STAGES = (
    ("eph_live_os", "EPH live-OS single node runs Cluster API and Metal3"),
    ("control_plane", "target cluster control plane machines are registered and booted"),
    ("pivot", "the target agent owns the cluster: BMH and Ironic data are pivoted"),
    ("workers", "worker machines join the data plane"),
    ("network", "CNI and data plane plugins are installed"),
)


def _network_manifests(spec: dict[str, Any], spec_path: Path | None) -> dict[str, dict[str, Any]]:
    inline = spec.get("manifests", {})
    files = spec.get("manifest_files", {})
    if not isinstance(inline, dict) or not isinstance(files, dict):
        raise network_plugins.NetworkStackError("NETWORK_STACK_MANIFESTS_INVALID")
    manifests = dict(inline)
    base = spec_path.parent if spec_path else Path.cwd()
    for name, filename in files.items():
        if not isinstance(name, str) or not isinstance(filename, str):
            raise network_plugins.NetworkStackError("NETWORK_STACK_MANIFESTS_INVALID")
        path = (base / filename).resolve()
        documents = [
            item
            for item in yaml.safe_load_all(path.read_text(encoding="utf-8"))
            if item is not None
        ]
        if not documents or any(not isinstance(item, dict) for item in documents):
            raise network_plugins.NetworkStackError("NETWORK_STACK_MANIFEST_INVALID")
        manifests[name] = (
            documents[0]
            if len(documents) == 1
            else {"apiVersion": "v1", "kind": "List", "items": documents}
        )
    return manifests


def command_network(args: argparse.Namespace) -> int:
    recorded_spec = args.dir / NETWORK_SPEC_FILE
    spec_path = args.spec or (recorded_spec if recorded_spec.is_file() else None)
    spec: dict[str, Any] = {}
    if spec_path:
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    plugins = network_plugins.plan_network_stack(spec)
    document: dict[str, Any] = {
        "status": "planned",
        "base_cni": next(
            (plugin.name for plugin in plugins if plugin.enabled and plugin.role == "cni"),
            None,
        ),
        "plugins": [
            {
                "name": plugin.name,
                "role": plugin.role,
                "enabled": plugin.enabled,
                "version": plugin.version,
                "config": plugin.config,
            }
            for plugin in plugins
        ],
        "data_plane_plugins": sorted(network_plugins.data_plane_plugins()),
    }
    args.dir.mkdir(parents=True, exist_ok=True)
    evidence_path = args.dir / NETWORK_FILE

    def write_evidence() -> None:
        evidence_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    if args.apply:
        namespace = spec.get("namespace", args.namespace)
        runner = _plain_runner(args.target_kubeconfig)
        applied = network_stack_adapter.apply_network_stack(
            plugins,
            namespace,
            runner,
            manifests=_network_manifests(spec, spec_path),
        )
        document["apply"] = applied
        document["status"] = "applied"
        if applied["apply_pending_plugins"]:
            document["status"] = "blocked"
            write_evidence()
            raise network_stack_adapter.NetworkStackAdapterError(
                "NETWORK_STACK_MANIFESTS_MISSING:" + ",".join(applied["apply_pending_plugins"])
            )
        write_evidence()
        if args.wait:
            readiness = network_stack_adapter.wait_for_network_stack_ready(
                plugins,
                namespace,
                runner,
                timeout_seconds=args.timeout_seconds,
            )
            document["readiness"] = readiness
            document["status"] = "ready"
    write_evidence()
    for plugin in plugins:
        state = "enabled" if plugin.enabled else "disabled"
        print(f"  {plugin.role:<10} {plugin.name:<8} {state}")
    print(f"network {document['status']}: {args.dir / NETWORK_FILE}")
    return 0


def command_target_kubeconfig(args: argparse.Namespace) -> int:
    plan = json.loads((args.dir / PLAN_FILE).read_text(encoding="utf-8"))
    cluster_name = plan.get("cluster_name")
    namespace = plan.get("namespace", "metal3")
    deadline = time.monotonic() + args.wait_seconds
    last_error = "target kubeconfig secret is not ready"
    while True:
        result = _plain_runner()(
            [
                "get",
                "secret",
                f"{cluster_name}-kubeconfig",
                "--namespace",
                namespace,
                "-o",
                "json",
            ],
            timeout=60,
        )
        try:
            if result.get("available") is True and result.get("returncode") == 0:
                secret = json.loads(result.get("stdout", ""))
                encoded = secret["data"]["value"]
                content = base64.b64decode(encoded, validate=True).decode("utf-8")
                document = yaml.safe_load(content)
                if not isinstance(document, dict) or not document.get("clusters"):
                    raise ValueError("kubeconfig has no cluster")
                destination = args.output or args.dir / TARGET_KUBECONFIG_FILE
                destination.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(content)
                print(f"target kubeconfig: {destination}")
                return 0
            last_error = str(result.get("stderr") or result.get("error") or last_error)
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            last_error = f"invalid target kubeconfig: {exc}"
        if time.monotonic() >= deadline:
            print(f"ERROR: {last_error}", file=sys.stderr)
            return 1
        time.sleep(args.poll_interval)


def _ready_artifact(path: Path, key: str = "status") -> bool:
    if not path.is_file():
        return False
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(document, dict) and document.get(key) == "ready"


def _platform_stage_state(
    plan_result: dict[str, Any],
    local_network_ready: bool,
) -> dict[str, bool]:
    dual = plan_result.get("dual_agent", {})
    source = plan_result.get("capi_pivot_source_preflight", {})
    postflight = plan_result.get("postflight", {})
    network = plan_result.get("network_stack", {})
    return {
        "eph_live_os": bool(plan_result),
        "control_plane": source.get("preflight_validated") is True,
        "pivot": (
            dual.get("pivot_completed") is True and dual.get("target_agent_owns_cluster") is True
        ),
        "workers": (
            dual.get("worker_provisioning_completed") is True
            or postflight.get("worker_readiness_verified") is True
        ),
        "network": local_network_ready or network.get("ready") is True,
    }


def _load_platform_deployment(deployment_id: str, settings_module: str) -> Any:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", settings_module)
    import django

    django.setup()
    from core.models import ClusterDeployment

    try:
        identifier = uuid.UUID(deployment_id)
    except ValueError as exc:
        raise ValueError("METAL3_DEPLOYMENT_ID_INVALID") from exc
    try:
        return ClusterDeployment.objects.get(pk=identifier)
    except ClusterDeployment.DoesNotExist as exc:
        raise ValueError("METAL3_DEPLOYMENT_NOT_FOUND") from exc


def command_stages(args: argparse.Namespace) -> int:
    plan_path = args.dir / PLAN_FILE
    if not plan_path.is_file():
        sys.stderr.write(f"ERROR: no plan recorded in {args.dir}\n")
        return 1
    document = json.loads(plan_path.read_text(encoding="utf-8"))
    roles = [item.get("role") for item in document.get("assignments", [])]
    control_plane = roles.count(fleet_planning.ROLE_CONTROL_PLANE)
    network_ready = _ready_artifact(args.dir / NETWORK_FILE)
    reached = {
        "eph_live_os": True,
        "control_plane": bool((args.dir / BMH_FILE).is_file() and control_plane),
        "pivot": (args.dir / "attestation.json").is_file(),
        "workers": _ready_artifact(args.dir / "workers.json"),
        "network": network_ready,
    }
    platform_status = None
    platform_error = None
    if args.deployment_id:
        deployment = _load_platform_deployment(args.deployment_id, args.django_settings)
        plan_result = deployment.plan_result if isinstance(deployment.plan_result, dict) else {}
        reached = _platform_stage_state(plan_result, network_ready)
        platform_status = deployment.status
        platform_error = deployment.error
        evidence = {
            "deployment_id": str(deployment.id),
            "status": platform_status,
            "phase": plan_result.get("dual_agent", {}).get("phase"),
            "stages": reached,
            "error": platform_error,
        }
        (args.dir / "platform-stages.json").write_text(
            json.dumps(evidence, indent=2) + "\n", encoding="utf-8"
        )
        print(f"platform deployment: {platform_status} phase={evidence['phase']}")
    for key, description in STAGES:
        mark = "done" if reached.get(key) else "pending"
        print(f"  {mark:<8} {key:<14} {description}")
    if platform_status == "failed":
        print(f"ERROR: platform deployment failed: {platform_error}", file=sys.stderr)
        return 1
    return 2 if args.require_ready and not all(reached.values()) else 0


def command_count(args: argparse.Namespace) -> int:
    plan_path = args.dir / PLAN_FILE
    if not plan_path.is_file():
        sys.stderr.write(f"ERROR: no plan recorded in {args.dir}\n")
        return 1
    document = json.loads(plan_path.read_text(encoding="utf-8"))
    print(len(document.get("assignments", [])))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan, render and deploy a Metal3 target cluster through the agent.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="reason about the fleet and render the manifests")
    plan.add_argument("--project", help="platform project id to read machines from")
    plan.add_argument("--cluster-name", required=True, help="target cluster name")
    plan.add_argument("--namespace", default="metal3")
    plan.add_argument("--control-plane-count", type=int, default=3, choices=(1, 3, 5, 7))
    plan.add_argument("--out-dir", type=Path, required=True)
    plan.add_argument("--control-plane-endpoint", help="VIP or DNS name of the target API server")
    plan.add_argument("--image-url", help="live-OS image the machines boot into")
    plan.add_argument("--k8s-version", default="v1.31.0")
    plan.add_argument(
        "--data-template", type=Path, help="Metal3DataTemplate content for the machines"
    )
    plan.add_argument(
        "--network-spec",
        type=Path,
        help="physical network and CNI/data-plane specification (YAML or JSON)",
    )
    plan.add_argument("--worker-node-labels", help="JSON object of pool -> label list")
    plan.add_argument("--django-settings", default="platform_api.settings")
    plan.add_argument(
        "--write-back",
        action="store_true",
        help="mirror the plan into AgentMachine.bmh_name/lifecycle_state and audit it",
    )
    plan.add_argument("--redfish-systems-path", default="/redfish/v1/Systems/1")
    plan.set_defaults(func=command_plan)

    apply = sub.add_parser("apply", help="hand the rendered bundle to the agent")
    apply.add_argument("--dir", type=Path, required=True)
    apply.add_argument("--namespace", default="metal3")
    apply.set_defaults(func=command_apply)

    verify = sub.add_parser("verify", help="judge the BMHs and the management cluster in python")
    verify.add_argument("--dir", type=Path, required=True)
    verify.add_argument("--namespace", default="metal3")
    verify.add_argument("--expected", type=int)
    verify.add_argument("--kubeconfig")
    verify.add_argument("--ironic-url", default=os.environ.get("IRONIC_URL"))
    verify.add_argument("--bmh-only", action="store_true", help="judge the BMHs and nothing else")
    verify.add_argument(
        "--no-host-checks",
        action="store_true",
        help="skip the provisioning-host bridges and Ironic probe",
    )
    verify.set_defaults(func=command_verify)

    explain = sub.add_parser("explain", help="print the recorded reasoning")
    explain.add_argument("--dir", type=Path, required=True)
    explain.set_defaults(func=command_explain)

    network = sub.add_parser(
        "network",
        help="plan the CNI and data plane plugins for the target cluster",
    )
    network.add_argument("--dir", type=Path, required=True)
    network.add_argument(
        "--spec",
        type=Path,
        help="network spec YAML/JSON, including plugins and manifests/manifest_files",
    )
    network.add_argument("--apply", action="store_true")
    network.add_argument("--wait", action="store_true")
    network.add_argument("--target-kubeconfig", type=Path)
    network.add_argument("--namespace", default="kube-system")
    network.add_argument("--timeout-seconds", type=int, default=900)
    network.set_defaults(func=command_network)

    kubeconfig = sub.add_parser(
        "target-kubeconfig", help="wait for and export the CAPI target kubeconfig secret"
    )
    kubeconfig.add_argument("--dir", type=Path, required=True)
    kubeconfig.add_argument("--output", type=Path)
    kubeconfig.add_argument("--wait-seconds", type=int, default=1800)
    kubeconfig.add_argument("--poll-interval", type=float, default=10)
    kubeconfig.set_defaults(func=command_target_kubeconfig)

    stages = sub.add_parser("stages", help="show which deployment stage the target cluster is at")
    stages.add_argument("--dir", type=Path, required=True)
    stages.add_argument("--require-ready", action="store_true")
    stages.add_argument("--deployment-id")
    stages.add_argument("--django-settings", default="platform_api.settings")
    stages.set_defaults(func=command_stages)

    count = sub.add_parser("count", help="print how many machines the recorded plan assigns")
    count.add_argument("--dir", type=Path, required=True)
    count.set_defaults(func=command_count)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.environ.setdefault("EPH_CAPI_NAMESPACE", getattr(args, "namespace", "metal3"))
    try:
        return int(args.func(args))
    except (fleet_planning.PlanningError, metal3_skill.Metal3SkillError, ValueError) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
