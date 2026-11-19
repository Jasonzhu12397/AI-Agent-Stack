"""Fetch once, then build every EPH/Agent image from verified local assets.

The online fetch phase locks image manifests and downloaded files. The offline
build phase never pulls packages or images: base images, Python wheels, CAPI
binaries, IPA archives and Metal3 images must already exist and match the lock.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .local_registry import target_registry_config

AGENT_DOCKERFILES = {
    "eph-agent": "Dockerfile.eph-agent",
    "target-agent": "Dockerfile.target-agent",
    "cloud-control-agent": "Dockerfile.cloud-control-agent",
}
KUBERNETES_IMAGES = {
    "kube-apiserver",
    "kube-controller-manager",
    "kube-scheduler",
    "kube-proxy",
    "etcd",
    "coredns",
    "pause",
}
Runner = Callable[[list[str]], str]


class OfflineImageError(RuntimeError):
    pass


def _run(arguments: list[str]) -> str:
    try:
        completed = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=1800,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OfflineImageError("OFFLINE_COMMAND_FAILED") from exc
    if completed.returncode != 0:
        raise OfflineImageError(f"OFFLINE_COMMAND_FAILED: {completed.stderr.strip()[:300]}")
    return completed.stdout


def _download(url: str, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        temporary.replace(destination)
    except (OSError, ValueError) as exc:
        temporary.unlink(missing_ok=True)
        raise OfflineImageError(f"OFFLINE_DOWNLOAD_FAILED: {url}") from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OfflineImageError("OFFLINE_MANIFEST_INVALID") from exc
    required = {
        "schema_version",
        "registry_endpoint",
        "base_image",
        "images",
        "binaries",
        "python_requirements",
        "ipa",
        "services",
    }
    if (
        not isinstance(document, dict)
        or set(document) != required
        or document["schema_version"] != 1
    ):
        raise OfflineImageError("OFFLINE_MANIFEST_INVALID")
    if not isinstance(document["images"], dict) or not document["images"]:
        raise OfflineImageError("OFFLINE_IMAGES_REQUIRED")
    for name, reference in {"base": document["base_image"], **document["images"]}.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9-]+", name):
            raise OfflineImageError("OFFLINE_IMAGE_NAME_INVALID")
        if not isinstance(reference, str) or not reference or any(c in reference for c in " \t\n"):
            raise OfflineImageError("OFFLINE_IMAGE_REFERENCE_INVALID")
    target_registry_config(document["registry_endpoint"])
    if not isinstance(document["python_requirements"], list) or not document["python_requirements"]:
        raise OfflineImageError("OFFLINE_PYTHON_REQUIREMENTS_REQUIRED")
    if not isinstance(document["services"], dict):
        raise OfflineImageError("OFFLINE_SERVICES_INVALID")
    return document


def _manifest_digest(manifest: dict[str, Any]) -> str:
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _image_archive(assets_dir: Path, name: str) -> Path:
    return assets_dir / "images" / f"{name}.tar"


def _inspect_archive(path: Path, runner: Runner) -> str:
    document = json.loads(runner(["skopeo", "inspect", f"docker-archive:{path}"]))
    digest = document.get("Digest")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise OfflineImageError("OFFLINE_IMAGE_DIGEST_INVALID")
    return digest


def _fetch_image(name: str, reference: str, archive: Path, runner: Runner) -> dict[str, str]:
    temporary = archive.with_suffix(".part")
    temporary.unlink(missing_ok=True)
    archive.parent.mkdir(parents=True, exist_ok=True)
    runner(
        [
            "skopeo",
            "copy",
            "--override-os",
            "linux",
            "--override-arch",
            "amd64",
            f"docker://{reference}",
            f"docker-archive:{temporary}:{reference}",
        ]
    )
    digest = _inspect_archive(temporary, runner)
    temporary.replace(archive)
    return {
        "source": reference,
        "archive": str(archive),
        "digest": digest,
        "sha256": sha256_file(archive),
    }


def fetch_assets(
    manifest_path: Path,
    assets_dir: Path,
    lock_path: Path,
    *,
    runner: Runner = _run,
    downloader: Callable[[str, Path], None] = _download,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    assets_dir.mkdir(parents=True, exist_ok=True)
    lock: dict[str, Any] = {
        "schema_version": 1,
        "manifest_sha256": _manifest_digest(manifest),
        "images": {},
        "binaries": {},
        "wheels": {},
    }
    lock["base_image"] = _fetch_image(
        "base", manifest["base_image"], _image_archive(assets_dir, "base"), runner
    )
    for name, reference in manifest["images"].items():
        lock["images"][name] = _fetch_image(
            name, reference, _image_archive(assets_dir, name), runner
        )

    binary_dir = assets_dir / "bin"
    binary_dir.mkdir(parents=True, exist_ok=True)
    for name, spec in manifest["binaries"].items():
        destination = binary_dir / name
        downloader(spec["url"], destination)
        lock["binaries"][name] = {
            "version": spec["version"],
            "url": spec["url"],
            "path": str(destination),
            "sha256": sha256_file(destination),
        }

    wheels_dir = assets_dir / "wheels"
    wheels_dir.mkdir(parents=True, exist_ok=True)
    runner(
        [
            os.environ.get("PYTHON", "python3"),
            "-m",
            "pip",
            "download",
            "--dest",
            str(wheels_dir),
            "--only-binary=:all:",
            "--platform",
            "manylinux2014_x86_64",
            "--python-version",
            "3.12",
            "--implementation",
            "cp",
            "--abi",
            "cp312",
            *manifest["python_requirements"],
        ]
    )
    wheels = sorted(wheels_dir.iterdir())
    if not wheels or any(not path.is_file() for path in wheels):
        raise OfflineImageError("OFFLINE_WHEELS_MISSING")
    lock["wheels"] = {path.name: sha256_file(path) for path in wheels}

    ipa_path = assets_dir / "ipa" / "ironic-python-agent.tar.gz"
    ipa_path.parent.mkdir(parents=True, exist_ok=True)
    downloader(manifest["ipa"]["url"], ipa_path)
    lock["ipa"] = {
        **manifest["ipa"],
        "path": str(ipa_path),
        "sha256": sha256_file(ipa_path),
    }
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = lock_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(lock_path)
    return lock


def _load_lock(lock_path: Path) -> dict[str, Any]:
    try:
        document = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OfflineImageError("OFFLINE_LOCK_MISSING_OR_INVALID") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise OfflineImageError("OFFLINE_LOCK_MISSING_OR_INVALID")
    return document


def _verify_file(path: Path, expected: str) -> None:
    if not path.is_file() or sha256_file(path) != expected:
        raise OfflineImageError(f"OFFLINE_ASSET_HASH_MISMATCH: {path}")


def _verify_assets(manifest: dict[str, Any], lock: dict[str, Any], assets_dir: Path) -> None:
    if lock.get("manifest_sha256") != _manifest_digest(manifest):
        raise OfflineImageError("OFFLINE_MANIFEST_CHANGED_REFETCH_REQUIRED")
    for item in [
        lock["base_image"],
        *lock["images"].values(),
        *lock["binaries"].values(),
        lock["ipa"],
    ]:
        _verify_file(Path(item.get("archive") or item["path"]), item["sha256"])
    wheels_dir = assets_dir / "wheels"
    for name, expected in lock["wheels"].items():
        _verify_file(wheels_dir / name, expected)
    if set(lock["images"]) != set(manifest["images"]):
        raise OfflineImageError("OFFLINE_LOCK_IMAGE_SET_MISMATCH")


def _link_or_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _build_agent(
    root: Path,
    name: str,
    dockerfile: str,
    output_dir: Path,
    runner: Runner,
) -> tuple[Path, str]:
    archive = output_dir / "images" / f"{name}.tar"
    metadata = output_dir / f"{name}.metadata.json"
    archive.parent.mkdir(parents=True, exist_ok=True)
    runner(
        [
            "docker",
            "buildx",
            "build",
            "--platform",
            "linux/amd64",
            "--pull=false",
            "--network=none",
            "--file",
            str(root / "deploy" / "offline" / dockerfile),
            "--tag",
            f"{name}:offline",
            "--output",
            f"type=docker,dest={archive}",
            "--metadata-file",
            str(metadata),
            str(root),
        ]
    )
    try:
        digest = json.loads(metadata.read_text(encoding="utf-8"))["containerimage.digest"]
    except (OSError, ValueError, KeyError) as exc:
        raise OfflineImageError("OFFLINE_BUILD_DIGEST_MISSING") from exc
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise OfflineImageError("OFFLINE_BUILD_DIGEST_INVALID")
    return archive, digest


def _target_repository(name: str) -> str:
    return "coredns/coredns" if name == "coredns" else name


def _target_reference(endpoint: str, name: str, source: str) -> str:
    repository = _target_repository(name)
    if name not in KUBERNETES_IMAGES:
        return f"{endpoint}/ai-agent-stack/{repository}:bootstrap"
    if "@" in source or ":" not in source:
        raise OfflineImageError("OFFLINE_KUBERNETES_IMAGE_TAG_REQUIRED")
    tag = source.rsplit(":", 1)[1]
    if not tag or "/" in tag:
        raise OfflineImageError("OFFLINE_KUBERNETES_IMAGE_TAG_INVALID")
    return f"{endpoint}/ai-agent-stack/{repository}:{tag}"


def render_bootstrap_images(
    manifest: dict[str, Any],
    lock: dict[str, Any],
    output_dir: Path,
    built: dict[str, tuple[Path, str]],
) -> dict[str, Any]:
    endpoint = manifest["registry_endpoint"]
    pinned = {
        name: f"{endpoint}/ai-agent-stack/{_target_repository(name)}@{item['digest']}"
        for name, item in lock["images"].items()
    }
    for name, (_, digest) in built.items():
        pinned[name] = f"{endpoint}/ai-agent-stack/{name}@{digest}"
    images = []
    for name, item in lock["images"].items():
        archive = output_dir / "images" / f"{name}.tar"
        _link_or_copy(Path(item["archive"]), archive)
        images.append(
            {
                "archive": str(archive),
                "source": item["source"],
                "target": _target_reference(endpoint, name, item["source"]),
            }
        )
    for name, (archive, _) in built.items():
        images.append(
            {
                "archive": str(archive),
                "source": f"{name}:offline",
                "target": f"{endpoint}/ai-agent-stack/{name}:bootstrap",
            }
        )
    services = copy.deepcopy(manifest["services"])
    for name, service in services.items():
        if name not in pinned:
            raise OfflineImageError("OFFLINE_SERVICE_IMAGE_MISSING")
        service.setdefault("container", {})["image"] = pinned[name]
    registry_archive = output_dir / "images" / "registry.tar"
    pause_reference = _target_reference(endpoint, "pause", lock["images"]["pause"]["source"])
    kubeadm_registry = target_registry_config(endpoint, pause_reference)
    ipa = dict(lock["ipa"])
    ipa_archive = output_dir / "ipa" / "ironic-python-agent.tar.gz"
    _link_or_copy(Path(ipa["path"]), ipa_archive)
    ipa["path"] = str(ipa_archive)
    return {
        "agent_image": pinned["eph-agent"],
        "cloud_control_image": pinned["cloud-control-agent"],
        "registry": {
            **kubeadm_registry,
            "endpoint": endpoint,
            "registry_image": {
                "archive": str(registry_archive),
                "reference": manifest["images"]["registry"],
            },
            "target_agent_image": pinned["target-agent"],
            "images": images,
            "services": services,
        },
        "ipa": ipa,
        "built_images": {name: pinned[name] for name in AGENT_DOCKERFILES},
        "upstream_images": {name: pinned[name] for name in lock["images"]},
    }


def build_offline(
    root: Path,
    manifest_path: Path,
    assets_dir: Path,
    lock_path: Path,
    output_dir: Path,
    *,
    endpoint: str | None = None,
    bootstrap_path: Path | None = None,
    runner: Runner = _run,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    lock = _load_lock(lock_path)
    _verify_assets(manifest, lock, assets_dir)
    if endpoint:
        target_registry_config(endpoint)
        manifest["registry_endpoint"] = endpoint
    runner(["docker", "load", "--input", str(assets_dir / "images" / "base.tar")])
    built = {
        name: _build_agent(root, name, dockerfile, output_dir, runner)
        for name, dockerfile in AGENT_DOCKERFILES.items()
    }
    result = render_bootstrap_images(manifest, lock, output_dir, built)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / "bootstrap-images.json"
    destination.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if bootstrap_path is not None:
        try:
            bootstrap = json.loads(bootstrap_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OfflineImageError("OFFLINE_BOOTSTRAP_INVALID") from exc
        if not isinstance(bootstrap, dict):
            raise OfflineImageError("OFFLINE_BOOTSTRAP_INVALID")
        bootstrap["agent_image"] = result["agent_image"]
        bootstrap["registry"] = result["registry"]
        bootstrap["registry_endpoint"] = result["registry"]["endpoint"]
        bootstrap["registry_pause_image"] = result["registry"]["pause_image"]
        bootstrap["ipa"] = result["ipa"]
        if isinstance(bootstrap.get("agent"), dict):
            bootstrap["agent"]["image"] = result["agent_image"]
        bootstrap_output = output_dir / "bootstrap.json"
        bootstrap_output.write_text(
            json.dumps(bootstrap, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        result["bootstrap_path"] = str(bootstrap_output)
        destination.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return result
