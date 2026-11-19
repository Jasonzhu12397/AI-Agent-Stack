"""Python lifecycle orchestration for the physical-machine Metal3 lab.

There is deliberately no shell runner above this module. The agent plans the
fleet, applies the target cluster and judges readiness through
:mod:`tools.metal3_deploy`; this module owns ordering, retries, evidence and the
small set of host-bootstrap stages that still change the provisioning host.

Only three vendored scripts remain executable stages: host package installation,
host network/ironic setup and management-cluster controller startup. They never
plan, render, apply or verify a BareMetalHost. Cleanup is implemented here too,
so neither ``run.sh`` nor ``make clean`` is part of the lifecycle.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.eph_agent import fleet_planning
from tools import metal3_deploy, vendor_import

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VENDOR_DIR = REPO_ROOT / "deploy" / "metal3-lab" / "vendor" / "metal3-dev-env"
DEFAULT_WORK_DIR = REPO_ROOT / ".metal3-lab"
MIN_CPUS = 4
MIN_MEMORY_KIB = 8 * 1024 * 1024
HOST_BOOTSTRAP_SCRIPTS = (
    "01_prepare_host.sh",
    "02_configure_host.sh",
    "03_launch_mgmt_cluster.sh",
)
INFRA_CONTAINERS = ("ipa-downloader", "httpd-infra", "ipxe-builder", "registry")
HOST_LINKS = ("ironic-peer", "ironicendpoint", "provisioning", "external")


class LabError(RuntimeError):
    """A lifecycle stage failed with evidence the operator can act on."""


@dataclass(frozen=True)
class LabSettings:
    vendor_dir: Path = DEFAULT_VENDOR_DIR
    work_dir: Path = DEFAULT_WORK_DIR
    hosts_dir: Path = DEFAULT_WORK_DIR / "hosts"
    cluster_name: str = "target-cluster"
    namespace: str = "metal3"
    control_plane_count: int = 3
    project: str | None = None
    control_plane_endpoint: str | None = None
    image_url: str | None = None
    k8s_version: str | None = None
    data_template: Path | None = None
    network_spec: Path | None = None
    auto_network: bool = False
    target_kubeconfig: Path | None = None
    target_ready_timeout: int = 1800
    network_ready_timeout: int = 900
    deployment_id: str | None = None
    deployment_ready_timeout: int = 3600
    django_settings: str = "platform_api.settings"
    write_back: bool = False
    allow_host_mutation: bool = False
    apply_retries: int = 10
    retry_interval: float = 60.0
    kubeconfig: str | None = None
    container_runtime: str = "docker"
    ironic_url: str | None = None
    vendor_source: str | None = None

    @property
    def artifacts_dir(self) -> Path:
        return self.work_dir / "artifacts"


@dataclass(frozen=True)
class HostStage:
    """One argv-only host action; no shell interprets this command."""

    name: str
    argv: tuple[str, ...]
    cwd: Path | None = None
    allow_failure: bool = False


@dataclass(frozen=True)
class PreflightReport:
    os_id: str
    os_version: str
    cpus: int
    memory_kib: int


class SubprocessExecutor:
    """Execute host stages without a shell and keep one log per stage."""

    def __init__(self, artifacts_dir: Path) -> None:
        self.artifacts_dir = artifacts_dir

    def __call__(self, stage: HostStage, environment: Mapping[str, str]) -> None:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.artifacts_dir / f"{stage.name}.log"
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"stage={stage.name}\nargv={json.dumps(stage.argv)}\n")
            log.flush()
            try:
                completed = subprocess.run(
                    list(stage.argv),
                    cwd=stage.cwd,
                    env=dict(environment),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
            except OSError as exc:
                raise LabError(f"{stage.name} could not start: {exc}; log {log_path}") from exc
        if completed.returncode != 0 and not stage.allow_failure:
            raise LabError(f"{stage.name} exited {completed.returncode}; inspect {log_path}")


def read_os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LabError(f"cannot identify the host operating system: {path}") from exc
    result: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator:
            result[key] = value.strip().strip('"')
    return result


def preflight_host(
    *,
    os_release: Mapping[str, str] | None = None,
    cpus: int | None = None,
    memory_kib: int | None = None,
    command_exists: Callable[[str], bool] | None = None,
) -> PreflightReport:
    """Fail before host mutation when the physical host cannot run the lab."""

    release = dict(os_release) if os_release is not None else read_os_release()
    if release.get("ID") != "ubuntu":
        raise LabError("the pinned Metal3 host workflow is validated only on Ubuntu")
    available_cpus = cpus if cpus is not None else (os.cpu_count() or 0)
    if available_cpus < MIN_CPUS:
        raise LabError(f"at least {MIN_CPUS} CPUs are required; found {available_cpus}")
    if memory_kib is None:
        try:
            meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
            memory_kib = int(
                next(
                    line.split()[1] for line in meminfo.splitlines() if line.startswith("MemTotal:")
                )
            )
        except (OSError, StopIteration, ValueError, IndexError) as exc:
            raise LabError("cannot read host memory from /proc/meminfo") from exc
    if memory_kib < MIN_MEMORY_KIB:
        raise LabError("at least 8 GiB RAM is required")
    exists = command_exists or (lambda name: shutil.which(name) is not None)
    missing = [name for name in ("sudo", "ip", "nproc") if not exists(name)]
    if missing:
        raise LabError(f"required host command(s) missing: {', '.join(missing)}")
    return PreflightReport(
        os_id=release["ID"],
        os_version=release.get("VERSION_ID", "unknown"),
        cpus=available_cpus,
        memory_kib=memory_kib,
    )


class Metal3Lab:
    """Own the complete lab lifecycle; all cluster decisions stay in python."""

    def __init__(
        self,
        settings: LabSettings,
        *,
        executor: Callable[[HostStage, Mapping[str, str]], None] | None = None,
        deploy: Callable[[Sequence[str]], int] = metal3_deploy.main,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.executor = executor or SubprocessExecutor(settings.artifacts_dir)
        self.deploy = deploy
        self.sleep = sleep

    def log(self, message: str) -> None:
        timestamp = datetime.now(UTC).isoformat(timespec="seconds")
        print(f"[{timestamp}] {message}", flush=True)

    def require_host_mutation_consent(self) -> None:
        if not self.settings.allow_host_mutation:
            raise LabError(
                "this action changes packages, networking and Kubernetes state; "
                "pass --allow-host-mutation or set METAL3_LAB_ALLOW_HOST_MUTATION=1"
            )

    def verify_vendor(self) -> dict[str, Any]:
        report = vendor_import.check_tree(self.settings.vendor_dir)
        for problem in report["problems"]:
            self.log(f"WARNING: intentional local vendor change: {problem}")
        self.log(
            f"vendored fork verified: {report['metadata']['file_count']} files at "
            f"upstream {report['metadata']['upstream_commit']}"
        )
        return report

    def host_environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment.update(
            {
                "NODES_PLATFORM": "baremetal",
                "BARE_METAL_HOSTS_DIR": str(self.settings.hosts_dir),
                "CAPM3_VERSION": environment.get("CAPM3_VERSION", "v1beta1"),
                "CAPI_VERSION": environment.get("CAPI_VERSION", "v1beta2"),
                "IMAGE_OS": environment.get("IMAGE_OS", "ubuntu"),
                "CONTAINER_RUNTIME": self.settings.container_runtime,
            }
        )
        if environment["IMAGE_OS"] != "ubuntu":
            raise LabError("only IMAGE_OS=ubuntu is supported")
        return environment

    def run_host_stage(self, stage: HostStage) -> None:
        self.log(f"host stage: {stage.name}")
        self.executor(stage, self.host_environment())

    def plan(self) -> int:
        settings = self.settings
        arguments: list[str] = [
            "plan",
            "--cluster-name",
            settings.cluster_name,
            "--namespace",
            settings.namespace,
            "--out-dir",
            str(settings.hosts_dir),
            "--control-plane-count",
            str(settings.control_plane_count),
            "--django-settings",
            settings.django_settings,
        ]
        optional = (
            ("--project", settings.project),
            ("--control-plane-endpoint", settings.control_plane_endpoint),
            ("--image-url", settings.image_url),
            ("--k8s-version", settings.k8s_version),
            ("--data-template", str(settings.data_template) if settings.data_template else None),
            ("--network-spec", str(settings.network_spec) if settings.network_spec else None),
        )
        for flag, value in optional:
            if value:
                arguments.extend((flag, value))
        if settings.write_back:
            arguments.append("--write-back")
        self.log(f"agent planning {settings.cluster_name} from the platform fleet")
        if self.deploy(arguments) != 0:
            raise LabError("the agent could not produce a target-cluster plan")
        return self.planned_machine_count()

    def planned_machine_count(self) -> int:
        path = self.settings.hosts_dir / metal3_deploy.PLAN_FILE
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            count = len(document["assignments"])
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise LabError(f"the recorded plan is unreadable: {path}") from exc
        if count < 1:
            raise LabError("the agent assigned no machine; inspect platform inventory")
        self.log(f"agent planned {count} physical machine(s)")
        return count

    def apply_target(self) -> None:
        attempts = self.settings.apply_retries
        if attempts < 1:
            raise LabError("apply_retries must be at least 1")
        arguments = [
            "apply",
            "--dir",
            str(self.settings.hosts_dir),
            "--namespace",
            self.settings.namespace,
        ]
        for attempt in range(1, attempts + 1):
            self.log(f"agent deploying target cluster (attempt {attempt}/{attempts})")
            if self.deploy(arguments) == 0:
                return
            if attempt < attempts:
                self.sleep(self.settings.retry_interval)
        raise LabError("the agent could not deploy the target cluster")

    def configure_target_network(self) -> None:
        settings = self.settings
        kubeconfig = settings.target_kubeconfig or (
            settings.hosts_dir / metal3_deploy.TARGET_KUBECONFIG_FILE
        )
        if (
            self.deploy(
                [
                    "target-kubeconfig",
                    "--dir",
                    str(settings.hosts_dir),
                    "--output",
                    str(kubeconfig),
                    "--wait-seconds",
                    str(settings.target_ready_timeout),
                ]
            )
            != 0
        ):
            raise LabError("the target API kubeconfig was not produced by CAPI")
        arguments = [
            "network",
            "--dir",
            str(settings.hosts_dir),
            "--apply",
            "--wait",
            "--target-kubeconfig",
            str(kubeconfig),
            "--timeout-seconds",
            str(settings.network_ready_timeout),
        ]
        if settings.network_spec:
            arguments.extend(("--spec", str(settings.network_spec)))
        self.log("agent applying and waiting for the target CNI/data-plane plugins")
        if self.deploy(arguments) != 0:
            raise LabError("target network installation or readiness failed")

    def wait_for_platform_deployment(self) -> None:
        settings = self.settings
        if not settings.deployment_id:
            return
        arguments = [
            "stages",
            "--dir",
            str(settings.hosts_dir),
            "--deployment-id",
            settings.deployment_id,
            "--django-settings",
            settings.django_settings,
            "--require-ready",
        ]
        deadline = time.monotonic() + settings.deployment_ready_timeout
        while True:
            result = self.deploy(arguments)
            if result == 0:
                self.log("platform pivot, workers and network evidence are ready")
                return
            if result == 1:
                raise LabError("platform deployment failed; inspect platform-stages.json")
            if time.monotonic() >= deadline:
                raise LabError("platform deployment did not become ready before the timeout")
            self.sleep(settings.retry_interval)

    def verify_target(self) -> None:
        arguments = [
            "verify",
            "--dir",
            str(self.settings.hosts_dir),
            "--namespace",
            self.settings.namespace,
            "--container-runtime",
            self.settings.container_runtime,
        ]
        if self.settings.kubeconfig:
            arguments.extend(("--kubeconfig", self.settings.kubeconfig))
        if self.settings.ironic_url:
            arguments.extend(("--ironic-url", self.settings.ironic_url))
        if self.deploy(arguments) != 0:
            raise LabError("target-cluster or management-cluster verification failed")

    def bootstrap_stages(self) -> list[HostStage]:
        return [
            HostStage(script.removesuffix(".sh"), (f"./{script}",), self.settings.vendor_dir)
            for script in HOST_BOOTSTRAP_SCRIPTS
        ]

    def prepare(self) -> None:
        self.require_host_mutation_consent()
        report = preflight_host()
        self.log(
            f"preflight passed: {report.os_id} {report.os_version}, "
            f"CPUs={report.cpus}, RAM_KiB={report.memory_kib}"
        )
        self.verify_vendor()
        self.plan()
        for stage in self.bootstrap_stages():
            self.run_host_stage(stage)
        self.apply_target()
        if self.settings.auto_network:
            self.configure_target_network()
        self.verify_target()
        if self.settings.auto_network:
            self.wait_for_platform_deployment()

    def verify(self) -> None:
        preflight_host()
        self.verify_vendor()
        self.planned_machine_count()
        self.verify_target()

    def cleanup_stages(self) -> list[HostStage]:
        settings = self.settings
        runtime = settings.container_runtime
        working_dir = Path(os.environ.get("WORKING_DIR", "/opt/metal3-dev-env"))
        ansible = (
            Path(os.environ.get("ANSIBLE_VENV", str(working_dir / "venv")))
            / "bin"
            / "ansible-playbook"
        )
        stages = [
            HostStage(
                "delete-kind-cluster",
                ("sudo", "-u", os.environ.get("USER", "root"), "kind", "delete", "cluster"),
                allow_failure=True,
            ),
            HostStage(
                "remove-infra-containers",
                ("sudo", runtime, "rm", "-f", *INFRA_CONTAINERS),
                allow_failure=True,
            ),
        ]
        if runtime == "podman":
            stages.append(
                HostStage(
                    "remove-infra-pods",
                    ("sudo", runtime, "pod", "rm", "-f", "ironic-pod", "infra-pod"),
                    allow_failure=True,
                )
            )
        stages.append(
            HostStage(
                "remove-firewall-rules",
                (
                    str(ansible),
                    "-e",
                    f"use_firewalld={os.environ.get('USE_FIREWALLD', 'false')}",
                    "-e",
                    "firewall_rule_state=absent",
                    "-i",
                    "vm-setup/inventory.ini",
                    "-b",
                    "-v",
                    "vm-setup/firewall.yml",
                ),
                settings.vendor_dir,
            )
        )
        stages.extend(
            HostStage(
                f"remove-link-{name}",
                ("sudo", "ip", "link", "delete", name),
                allow_failure=True,
            )
            for name in HOST_LINKS
        )
        return stages

    def clean(self) -> None:
        self.require_host_mutation_consent()
        preflight_host()
        self.verify_vendor()
        for stage in self.cleanup_stages():
            self.run_host_stage(stage)
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        shutil.rmtree(config_home / "cluster-api", ignore_errors=True)

    def reference(self) -> Path:
        target = self.settings.vendor_dir.with_name(self.settings.vendor_dir.name + ".upstream")
        source = self.settings.vendor_source or vendor_import.ARCHIVE_URL
        vendor_import.import_tree(source, target)
        self.log(f"upstream reference imported into {target}; the running fork is untouched")
        return target

    def reseal(self) -> None:
        vendor_import.reseal_tree(self.settings.vendor_dir)

    def run(self, action: str) -> None:
        actions = {
            "prepare": self.prepare,
            "auto": self.prepare,
            "verify": self.verify,
            "clean": self.clean,
            "reference": self.reference,
            "reseal": self.reseal,
        }
        try:
            handler = actions[action]
        except KeyError as exc:
            raise LabError(f"unknown lifecycle action: {action}") from exc
        handler()


def settings_from_namespace(args: Any) -> LabSettings:
    work_dir = Path(args.work_dir or os.environ.get("METAL3_LAB_WORK_DIR", DEFAULT_WORK_DIR))
    return LabSettings(
        vendor_dir=Path(
            args.vendor_dir or os.environ.get("METAL3_LAB_VENDOR_DIR", DEFAULT_VENDOR_DIR)
        ),
        work_dir=work_dir,
        hosts_dir=Path(
            args.hosts_dir or os.environ.get("METAL3_LAB_HOSTS_DIR", work_dir / "hosts")
        ),
        cluster_name=args.cluster_name
        or os.environ.get("METAL3_LAB_CLUSTER_NAME", "target-cluster"),
        namespace=args.namespace,
        control_plane_count=args.control_plane_count
        or int(os.environ.get("METAL3_LAB_CONTROL_PLANE_COUNT", "3")),
        project=args.project or os.environ.get("METAL3_LAB_PROJECT_ID"),
        control_plane_endpoint=args.control_plane_endpoint
        or os.environ.get("METAL3_LAB_CONTROL_PLANE_ENDPOINT"),
        image_url=args.image_url or os.environ.get("METAL3_LAB_IMAGE_URL"),
        k8s_version=args.k8s_version or os.environ.get("METAL3_LAB_K8S_VERSION"),
        data_template=Path(args.data_template) if args.data_template else None,
        network_spec=(Path(args.network_spec) if getattr(args, "network_spec", None) else None),
        auto_network=getattr(args, "auto_network", False) or getattr(args, "action", "") == "auto",
        target_kubeconfig=(
            Path(args.target_kubeconfig) if getattr(args, "target_kubeconfig", None) else None
        ),
        target_ready_timeout=getattr(args, "target_ready_timeout", 1800),
        network_ready_timeout=getattr(args, "network_ready_timeout", 900),
        deployment_id=getattr(args, "deployment_id", None),
        deployment_ready_timeout=getattr(args, "deployment_ready_timeout", 3600),
        django_settings=args.django_settings,
        write_back=(
            args.write_back
            or getattr(args, "action", "") == "auto"
            or os.environ.get("METAL3_LAB_WRITE_BACK") == "1"
        ),
        allow_host_mutation=args.allow_host_mutation
        or os.environ.get("METAL3_LAB_ALLOW_HOST_MUTATION") == "1",
        apply_retries=args.apply_retries,
        retry_interval=args.retry_interval,
        kubeconfig=args.kubeconfig,
        container_runtime=args.container_runtime,
        ironic_url=args.ironic_url or os.environ.get("IRONIC_URL"),
        vendor_source=args.vendor_source,
    )


def validate_settings(settings: LabSettings) -> None:
    if settings.control_plane_count not in fleet_planning.VALID_CONTROL_PLANE_COUNTS:
        raise LabError(
            f"control plane size must be one of {fleet_planning.VALID_CONTROL_PLANE_COUNTS}"
        )
    if settings.container_runtime not in {"docker", "podman"}:
        raise LabError("container runtime must be docker or podman")
    if settings.retry_interval < 0:
        raise LabError("retry interval cannot be negative")
    if settings.auto_network:
        if not settings.project:
            raise LabError("auto requires --project so write-back is tenant scoped")
        if settings.network_spec is None:
            raise LabError("auto network requires --network-spec with plugin manifests")
        if not settings.control_plane_endpoint or not settings.image_url:
            raise LabError("auto requires --control-plane-endpoint and --image-url")
        if not 60 <= settings.target_ready_timeout <= 7200:
            raise LabError("target ready timeout must be between 60 and 7200 seconds")
        if not 60 <= settings.network_ready_timeout <= 3600:
            raise LabError("network ready timeout must be between 60 and 3600 seconds")
        if not 60 <= settings.deployment_ready_timeout <= 86400:
            raise LabError("deployment ready timeout must be between 60 and 86400 seconds")
