import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from apps.eph_agent import bootstrap_renderer, capi_renderer, local_registry
from apps.eph_agent.metal3_skill import Metal3SkillError

ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = "10.0.0.10:5000"


def _image(repository, character):
    return f"{ENDPOINT}/ai-agent-stack/{repository}@sha256:{character * 64}"


def _registry():
    return {
        "endpoint": ENDPOINT,
        "registry_image": {"archive": "/images/registry.tar", "reference": "registry:2"},
        "target_agent_image": _image("target-agent", "b"),
        "images": [
            {
                "archive": f"/images/{name}.tar",
                "source": f"{name}:local",
                "target": f"{ENDPOINT}/ai-agent-stack/{name}:bootstrap",
            }
            for name in ("eph-agent", "target-agent", "vbmc", "sushy-tools", "httpd-infra")
        ],
        "services": {
            "vbmc": {
                "container": {
                    "image": _image("vbmc", "c"),
                    "ports": [{"containerPort": 6240, "hostPort": 6240}],
                },
            },
            "sushy-tools": {
                "container": {
                    "image": _image("sushy-tools", "d"),
                    "ports": [{"containerPort": 8000, "hostPort": 8000}],
                },
            },
            "httpd-infra": {
                "container": {
                    "image": _image("httpd-infra", "e"),
                    "ports": [{"containerPort": 80, "hostPort": 80}],
                    "volumeMounts": [{"name": "shared", "mountPath": "/shared"}],
                },
                "pod": {
                    "hostNetwork": True,
                    "volumes": [{"name": "shared", "hostPath": {"path": "/opt/metal3-data"}}],
                },
            },
        },
    }


def _spec():
    return {
        "node_name": "eph-01",
        "kubernetes_version": "v1.31.0",
        "node_ip": "10.0.0.10",
        "pod_cidr": "10.244.0.0/16",
        "service_cidr": "10.96.0.0/12",
        "agent": {
            "control_url": "https://control.example.test",
            "image": _image("eph-agent", "a"),
            "mtls_secret_ref": "eph-agent-mtls",
            "agent_id": "0b8a5dc2-6f6f-4b4e-9a4c-3f6a1b7c8d90",
            "installation_id": "1f0c2a44-8b1d-4c3e-8f5a-2b6c9d0e1f22",
        },
        "registry": _registry(),
    }


def test_cloud_init_preloads_images_then_runs_registry_and_bmc_as_pods_after_kubeadm():
    seed = bootstrap_renderer.render_cloud_init_seed(_spec())["user-data"]
    assert "docker.io" not in seed["packages"]
    assert "docker-compose-v2" not in seed["packages"]
    assert "command -v kubeadm" in seed["runcmd"][0]
    assert seed["runcmd"][1].endswith("--preload-only")
    assert seed["runcmd"][2].startswith("kubeadm init")
    assert seed["runcmd"][4].startswith("python3 /usr/local/lib/ai-agent-stack/local_registry.py")
    files = {item["path"]: item["content"] for item in seed["write_files"]}
    assert "/etc/ai-agent-stack/docker-compose.yaml" not in files
    registry_docs = list(yaml.safe_load_all(files["/etc/ai-agent-stack/registry.yaml"]))
    registry = next(item for item in registry_docs if item["kind"] == "Deployment")
    registry_container = registry["spec"]["template"]["spec"]["containers"][0]
    assert registry_container["imagePullPolicy"] == "Never"
    assert registry["spec"]["template"]["spec"]["hostNetwork"] is True
    service_docs = list(yaml.safe_load_all(files["/etc/ai-agent-stack/eph-bmc-services.yaml"]))
    assert {item["kind"] for item in service_docs} == {"Deployment"}
    assert {item["metadata"]["name"] for item in service_docs} == {
        "vbmc",
        "sushy-tools",
        "httpd-infra",
    }
    config = json.loads(files["/etc/ai-agent-stack/registry.json"])
    assert config["target_agent_image"] == _image("target-agent", "b")
    assert len(config["images"]) == 5
    hosts = files[f"/etc/containerd/certs.d/{ENDPOINT}/hosts.toml"]
    assert f'server = "http://{ENDPOINT}"' in hosts


def test_local_registry_rejects_external_target_agent_registry():
    spec = _spec()
    spec["registry"]["target_agent_image"] = spec["registry"]["target_agent_image"].replace(
        ENDPOINT, "registry.example.test:5000"
    )
    with pytest.raises(Metal3SkillError) as error:
        bootstrap_renderer.render_cloud_init_seed(spec)
    assert str(error.value) == "EPH_TARGET_AGENT_IMAGE_MUST_USE_LOCAL_REGISTRY"


def test_a_physical_fleet_does_not_need_the_bmc_emulators():
    spec = _spec()
    for name in ("vbmc", "sushy-tools"):
        del spec["registry"]["services"][name]
    rendered = local_registry.render_local_registry(
        spec["registry"], node_ip="10.0.0.10", agent_image=spec["agent"]["image"]
    )

    assert rendered["config"]["service_names"] == ["httpd-infra"]
    assert rendered["config"]["emulated_bmc"] is False
    assert rendered["config"]["pinned_images"]


def test_half_a_bmc_emulator_pair_is_rejected():
    spec = _spec()
    del spec["registry"]["services"]["vbmc"]
    with pytest.raises(Metal3SkillError) as error:
        local_registry.render_local_registry(
            spec["registry"], node_ip="10.0.0.10", agent_image=spec["agent"]["image"]
        )
    assert str(error.value) == "EPH_LOCAL_BMC_SERVICES_INCOMPLETE"


def test_the_boot_artifact_server_is_still_required():
    spec = _spec()
    for name in ("vbmc", "sushy-tools", "httpd-infra"):
        del spec["registry"]["services"][name]
    with pytest.raises(Metal3SkillError) as error:
        local_registry.render_local_registry(
            spec["registry"], node_ip="10.0.0.10", agent_image=spec["agent"]["image"]
        )
    assert str(error.value) == "EPH_LOCAL_BMC_SERVICES_REQUIRED"


def test_runtime_imports_pushes_and_waits_for_all_local_pods(monkeypatch):
    module_path = ROOT / "eph-iso" / "local_registry.py"
    spec = importlib.util.spec_from_file_location("eph_local_registry_runtime", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    commands = []

    def fake_run(arguments, timeout=300):
        commands.append(arguments)
        return ""

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module, "wait_for_kubernetes", lambda *args: None)
    monkeypatch.setattr(module, "wait_for_registry", lambda *args: None)
    config = local_registry.render_local_registry(
        _registry(), node_ip="10.0.0.10", agent_image=_image("eph-agent", "a")
    )["config"]
    digests = {
        item["target"]: item["reference"].split("@", 1)[1] for item in config["pinned_images"]
    }
    monkeypatch.setattr(module, "published_digest", lambda target: digests[target])
    module.publish(config)
    pushes = [command for command in commands if command[3:5] == ["images", "push"]]
    imports = [command for command in commands if command[3:5] == ["images", "import"]]
    rollouts = [command for command in commands if "rollout" in command]
    applies = [command for command in commands if command[-2] == "-f"]
    assert len(pushes) == 5
    assert len(imports) == 6
    assert len(rollouts) == 4
    assert [command[-1] for command in applies] == [
        config["registry_manifest"],
        config["services_manifest"],
    ]
    assert all(command[0] != "docker" for command in commands)

    commands.clear()
    module.preload(config)
    assert len([command for command in commands if command[3:5] == ["images", "import"]]) == 6
    assert not any(command[3:5] == ["images", "push"] for command in commands)
    assert not any(command[0] == "kubectl" for command in commands)


def test_target_control_plane_and_workers_receive_local_registry_configuration():
    template = {
        "meta_data": {},
        "network_data": {
            "links": {"ethernets": [{"type": "phy", "id": "eno1"}]},
            "networks": {"ipv4DHCP": [{"id": "control", "link": "eno1"}]},
        },
    }
    documents = capi_renderer.render_capi_metal3_manifests(
        {
            "name": "target",
            "namespace": "metal3",
            "control_plane_endpoint": "10.0.0.100",
            "image_url": "https://images.example.test/ephemeral.raw",
            "control_plane_count": 1,
            "control_plane_data_template": template,
            "registry_endpoint": ENDPOINT,
            "registry_pause_image": f"{ENDPOINT}/ai-agent-stack/pause:3.10.1",
            "worker_pools": [{"name": "workers", "count": 1, "data_template": template}],
        }
    )
    control_plane = next(item for item in documents if item["kind"] == "KubeadmControlPlane")
    workers = next(item for item in documents if item["kind"] == "KubeadmConfigTemplate")
    control_files = control_plane["spec"]["kubeadmConfigSpec"]["files"]
    cluster_config = control_plane["spec"]["kubeadmConfigSpec"]["clusterConfiguration"]
    init_config = control_plane["spec"]["kubeadmConfigSpec"]["initConfiguration"]
    assert init_config["skipPhases"] == ["addon/kube-proxy"]
    control_commands = control_plane["spec"]["kubeadmConfigSpec"]["preKubeadmCommands"]
    worker_commands = workers["spec"]["template"]["spec"]["preKubeadmCommands"]
    assert "command -v kubeadm" in control_commands[0]
    assert "command -v kubeadm" in worker_commands[0]
    assert cluster_config["imageRepository"] == f"{ENDPOINT}/ai-agent-stack"
    assert cluster_config["dns"]["imageRepository"] == f"{ENDPOINT}/ai-agent-stack/coredns"
    worker_files = workers["spec"]["template"]["spec"]["files"]
    for files in (control_files, worker_files):
        containerd = next(item for item in files if item["path"] == "/etc/containerd/config.toml")
        assert f'sandbox_image = "{ENDPOINT}/ai-agent-stack/pause:3.10.1"' in containerd["content"]
        hosts = next(item for item in files if item["path"].endswith("hosts.toml"))
        assert ENDPOINT in hosts["path"]
        assert f'server = "http://{ENDPOINT}"' in hosts["content"]


def test_agent_build_and_deploy_never_use_an_external_registry():
    build = (ROOT / "deploy" / "agents" / "build-agents.sh").read_text(encoding="utf-8")
    deploy = (ROOT / "deploy" / "agents" / "deploy.sh").read_text(encoding="utf-8")
    assert "--push" not in build
    assert "type=docker,dest=" in build
    assert "TARGET_AGENT_IMAGE_MUST_USE_EPH_LOCAL_REGISTRY" in deploy
    assert "CLOUD_CONTROL_IMAGE_MUST_USE_EPH_LOCAL_REGISTRY" in deploy

def test_a_bmc_port_that_is_not_published_on_the_node_is_refused():
    # A virtual BMC has to answer on the control address before any host boots;
    # a port that only exists in the pod network cannot be reached from there.
    spec = _spec()
    spec["registry"]["services"]["vbmc"]["container"]["ports"] = [
        {"containerPort": 6240}
    ]
    with pytest.raises(Metal3SkillError) as error:
        local_registry.render_local_registry(
            spec["registry"], node_ip="10.0.0.10", agent_image=spec["agent"]["image"]
        )
    assert str(error.value) == "EPH_LOCAL_BMC_SERVICE_PORT_NOT_PUBLISHED"


def test_the_host_network_counts_as_a_published_port():
    spec = _spec()
    spec["registry"]["services"]["vbmc"]["pod"] = {"hostNetwork": True}
    del spec["registry"]["services"]["vbmc"]["container"]["ports"]
    rendered = local_registry.render_local_registry(
        spec["registry"], node_ip="10.0.0.10", agent_image=spec["agent"]["image"]
    )
    vbmc = next(
        item for item in rendered["service_documents"] if item["metadata"]["name"] == "vbmc"
    )
    assert vbmc["spec"]["template"]["spec"]["hostNetwork"] is True
