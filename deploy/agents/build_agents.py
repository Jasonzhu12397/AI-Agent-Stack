"""Build the three agent images with digest-pinned output.

Python replacement for the former ``deploy/agents/build-agents.sh``. Behaviour
is identical: the same environment variables drive it, the same validation
error codes are printed on failure, and the same ``<NAME>_IMAGE_REF`` /
``<NAME>_IMAGE_ARCHIVE`` lines are emitted on stdout for the release workflow.

Usage:
    python3 deploy/agents/build_agents.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]

REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+(/[A-Za-z0-9._-]+)+$")
TAG_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
VERSION_PATTERN = re.compile(r"^v[0-9]+[.][0-9]+[.][0-9]+$")
SHA256_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")
DIGEST_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def fail(code: str) -> None:
    print(code, file=sys.stderr)
    raise SystemExit(1)


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def require(condition: bool, code: str) -> None:
    if not condition:
        fail(code)


def build_image(
    name: str,
    repository: str,
    dockerfile: str,
    image_tag: str,
    platforms: str,
    image_output_dir: Path,
    build_args: list[str],
) -> None:
    metadata_file = image_output_dir / f"{name}.metadata.json"
    archive_file = image_output_dir / f"{name}.tar"
    run(
        [
            "docker",
            "buildx",
            "build",
            "--platform",
            platforms,
            "--file",
            str(ROOT_DIR / dockerfile),
            *build_args,
            "--tag",
            f"{repository}:{image_tag}",
            "--output",
            f"type=docker,dest={archive_file}",
            "--metadata-file",
            str(metadata_file),
            str(ROOT_DIR),
        ]
    )
    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    digest = metadata.get("containerimage.digest", "")
    require(bool(DIGEST_PATTERN.match(digest)), f"{name}_IMAGE_DIGEST_INVALID")
    print(f"{name}_IMAGE_REF={repository}@{digest}")
    print(f"{name}_IMAGE_ARCHIVE={archive_file}")


def main() -> None:
    image_tag = env("IMAGE_TAG")
    image_output_dir = Path(
        env("IMAGE_OUTPUT_DIR", str(ROOT_DIR / "deploy" / "agents" / "dist"))
    )
    cloud_control_repository = env("CLOUD_CONTROL_IMAGE_REPOSITORY")
    eph_agent_repository = env("EPH_AGENT_IMAGE_REPOSITORY")
    target_agent_repository = env("TARGET_AGENT_IMAGE_REPOSITORY")
    platforms = env("PLATFORMS", "linux/amd64")
    clusterctl_version = env("CLUSTERCTL_VERSION")
    clusterctl_sha256 = env("CLUSTERCTL_SHA256_AMD64")
    kubectl_version = env("KUBECTL_VERSION")
    kubectl_sha256 = env("KUBECTL_SHA256_AMD64")

    for repository, code in (
        (cloud_control_repository, "CLOUD_CONTROL_IMAGE_REPOSITORY_INVALID"),
        (eph_agent_repository, "EPH_AGENT_IMAGE_REPOSITORY_INVALID"),
        (target_agent_repository, "TARGET_AGENT_IMAGE_REPOSITORY_INVALID"),
    ):
        require(bool(REPOSITORY_PATTERN.match(repository)), code)
    require(bool(TAG_PATTERN.match(image_tag)), "IMAGE_TAG_INVALID")
    require(image_tag != "latest", "MUTABLE_LATEST_TAG_NOT_ALLOWED")
    require(platforms == "linux/amd64", "ONLY_LINUX_AMD64_IS_CURRENTLY_VERIFIED")
    require(bool(VERSION_PATTERN.match(clusterctl_version)), "CLUSTERCTL_VERSION_INVALID")
    require(bool(VERSION_PATTERN.match(kubectl_version)), "KUBECTL_VERSION_INVALID")
    require(bool(SHA256_PATTERN.match(clusterctl_sha256)), "CLUSTERCTL_SHA256_AMD64_INVALID")
    require(bool(SHA256_PATTERN.match(kubectl_sha256)), "KUBECTL_SHA256_AMD64_INVALID")

    run(["docker", "buildx", "version"])
    image_output_dir.mkdir(parents=True, exist_ok=True)

    build_image(
        "CLOUD_CONTROL_AGENT",
        cloud_control_repository,
        "Dockerfile.cloud-control-agent",
        image_tag,
        platforms,
        image_output_dir,
        [],
    )
    build_image(
        "EPH_AGENT",
        eph_agent_repository,
        "Dockerfile.eph-agent",
        image_tag,
        platforms,
        image_output_dir,
        [
            "--build-arg",
            f"CLUSTERCTL_VERSION={clusterctl_version}",
            "--build-arg",
            f"CLUSTERCTL_SHA256_AMD64={clusterctl_sha256}",
            "--build-arg",
            f"KUBECTL_VERSION={kubectl_version}",
            "--build-arg",
            f"KUBECTL_SHA256_AMD64={kubectl_sha256}",
        ],
    )
    build_image(
        "TARGET_AGENT",
        target_agent_repository,
        "Dockerfile.target-agent",
        image_tag,
        platforms,
        image_output_dir,
        [
            "--build-arg",
            f"KUBECTL_VERSION={kubectl_version}",
            "--build-arg",
            f"KUBECTL_SHA256_AMD64={kubectl_sha256}",
        ],
    )


if __name__ == "__main__":
    main()
