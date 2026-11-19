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
            for name in ("eph-agent", "target-agent", "vbmc", "sushy-tools")
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


def test_cloud_init_runs_registry_and_eph_bmc_services_as_pods_after_kubeadm():
    seed = bootstrap_renderer.render_cloud_init_seed(_spec())["user-data"]
    assert "docker.io" not in seed["packages"]
    assert "docker-compose-v2" not in seed["packages"]
    assert seed["runcmd"][0].startswith("kubeadm init")
    assert seed["runcmd"][2].startswith("python3 /usr/local/lib/ai-agent-stack/local_registry.py")
    files = {item["path"]: item["content"] for item in seed["write_files"]}
    assert "/etc/ai-agent-stack/docker-compose.yaml" not in files
    registry_docs = list(yaml.safe_load_all(files["/etc/ai-agent-stack/registry.yaml"]))
    registry = next(item for item in registry_docs if item["kind"] == "Deployment")
    registry_container = registry["spec"]["template"]["spec"]["containers"][0]
    assert registry_container["imagePullPolicy"] == "Never"
    assert registry["spec"]["template"]["spec"]["hostNetwork"] is True
    service_docs = list(yaml.safe_load_all(files["/etc/ai-agent-stack/eph-bmc-services.yaml"]))
    assert {item["kind"] for item in service_docs} == {"Deployment"}
    assert {item["metadata"]["name"] for item in service_docs} == {"vbmc", "sushy-tools"}
    config = json.loads(files["/etc/ai-agent-stack/registry.json"])
    assert config["target_agent_image"] == _image("target-agent", "b")
    assert len(config["images"]) == 4
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


def test_local_registry_requires_vbmc_and_sushy_tools():
    spec = _spec()
    del spec["registry"]["services"]["vbmc"]
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
    assert len(pushes) == 4
    assert len(imports) == 5
    assert len(rollouts) == 3
    assert [command[-1] for command in applies] == [
        config["registry_manifest"],
        config["services_manifest"],
    ]
    assert all(command[0] != "docker" for command in commands)


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
            "worker_pools": [{"name": "workers", "count": 1, "data_template": template}],
        }
    )
    control_plane = next(item for item in documents if item["kind"] == "KubeadmControlPlane")
    workers = next(item for item in documents if item["kind"] == "KubeadmConfigTemplate")
    control_files = control_plane["spec"]["kubeadmConfigSpec"]["files"]
    worker_files = workers["spec"]["template"]["spec"]["files"]
    for files in (control_files, worker_files):
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
