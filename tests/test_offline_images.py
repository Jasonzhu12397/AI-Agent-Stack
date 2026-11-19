import json
from pathlib import Path

import pytest

from apps.eph_agent import local_registry, offline_images

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deploy" / "offline" / "images.json"


def _download(url, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(("offline:" + url).encode())


def _fake_runner(commands):
    def run(arguments):
        commands.append(arguments)
        if arguments[:2] == ["skopeo", "copy"]:
            archive_argument = arguments[-1]
            archive = Path(archive_argument.removeprefix("docker-archive:").split(":", 1)[0])
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(b"verified-image-archive")
            return ""
        if arguments[:2] == ["skopeo", "inspect"]:
            return json.dumps({"Digest": "sha256:" + "a" * 64})
        if arguments[:3] == ["python3", "-m", "pip"] or arguments[1:3] == ["-m", "pip"]:
            wheels = Path(arguments[arguments.index("--dest") + 1])
            wheels.mkdir(parents=True, exist_ok=True)
            (wheels / "offline-1.0-py3-none-any.whl").write_bytes(b"wheel")
            return ""
        if arguments[:2] == ["docker", "buildx"]:
            output = arguments[arguments.index("--output") + 1]
            archive = Path(output.removeprefix("type=docker,dest="))
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(b"built-agent-archive")
            metadata = Path(arguments[arguments.index("--metadata-file") + 1])
            name = Path(arguments[arguments.index("--tag") + 1]).name.split(":")[0]
            character = {"eph-agent": "b", "target-agent": "c", "cloud-control-agent": "d"}[name]
            metadata.write_text(json.dumps({"containerimage.digest": "sha256:" + character * 64}))
            return ""
        return ""

    return run


def test_fetch_then_build_is_fully_offline_and_generates_registry_seed(tmp_path):
    fetch_commands = []
    assets = tmp_path / "assets"
    lock = assets / "lock.json"
    offline_images.fetch_assets(
        MANIFEST,
        assets,
        lock,
        runner=_fake_runner(fetch_commands),
        downloader=_download,
    )
    assert lock.is_file()
    assert (assets / "ipa" / "ironic-python-agent.tar.gz").is_file()
    assert (assets / "images" / "ironic.tar").is_file()
    assert (assets / "bin" / "clusterctl").is_file()

    build_commands = []
    output = tmp_path / "output"
    bootstrap = tmp_path / "bootstrap-input.json"
    bootstrap.write_text(json.dumps({"instance_id": "eph-01", "agent": {"image": "old"}}))
    result = offline_images.build_offline(
        ROOT,
        MANIFEST,
        assets,
        lock,
        output,
        bootstrap_path=bootstrap,
        runner=_fake_runner(build_commands),
    )
    builds = [item for item in build_commands if item[:2] == ["docker", "buildx"]]
    assert len(builds) == 3
    assert all("--network=none" in item and "--pull=false" in item for item in builds)
    assert not any(item[0] in {"skopeo", "curl", "wget"} for item in build_commands)
    assert (
        len(result["registry"]["images"])
        == len(offline_images.load_manifest(MANIFEST)["images"]) + 3
    )
    assert Path(result["ipa"]["path"]).parent == output / "ipa"
    coredns = next(
        item
        for item in result["registry"]["images"]
        if item["source"].startswith("registry.k8s.io/coredns/")
    )
    assert coredns["target"].endswith("/ai-agent-stack/coredns/coredns:v1.13.1")
    assert result["registry"]["pause_image"].endswith("/ai-agent-stack/pause:3.10.1")
    assert result["registry"]["image_repository"] == "10.0.0.10:5000/ai-agent-stack"
    rendered = local_registry.render_local_registry(
        result["registry"], node_ip="10.0.0.10", agent_image=result["agent_image"]
    )
    assert {item["metadata"]["name"] for item in rendered["service_documents"]} == {
        "vbmc",
        "sushy-tools",
        "httpd-infra",
    }
    assert (output / "bootstrap-images.json").is_file()
    merged = json.loads((output / "bootstrap.json").read_text())
    assert merged["instance_id"] == "eph-01"
    assert merged["agent"]["image"] == result["agent_image"]
    assert merged["registry"] == result["registry"]


def test_offline_build_rejects_changed_versions_until_refetch(tmp_path):
    assets = tmp_path / "assets"
    offline_images.fetch_assets(
        MANIFEST,
        assets,
        assets / "lock.json",
        runner=_fake_runner([]),
        downloader=_download,
    )
    changed = tmp_path / "changed.json"
    manifest = offline_images.load_manifest(MANIFEST)
    manifest["binaries"]["kubectl"]["version"] = "v1.99.0"
    changed.write_text(json.dumps(manifest))
    with pytest.raises(offline_images.OfflineImageError, match="REFETCH_REQUIRED"):
        offline_images.build_offline(
            ROOT,
            changed,
            assets,
            assets / "lock.json",
            tmp_path / "output",
            runner=_fake_runner([]),
        )


def test_offline_dockerfiles_never_download_during_build():
    for path in (ROOT / "deploy" / "offline").glob("Dockerfile.*"):
        content = path.read_text(encoding="utf-8")
        assert "--no-index" in content
        assert "apt-get" not in content
        assert "curl " not in content
        assert "wget " not in content
