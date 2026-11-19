#!/usr/bin/env python3
"""Start the EPH local registry and publish the ISO's pinned images.

The input is rendered by apps.eph_agent.local_registry. Every subprocess uses
an argument vector; no shell command or external registry is involved.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


class RegistryError(RuntimeError):
    pass


def run(arguments: list[str], *, timeout: int = 300) -> str:
    try:
        completed = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RegistryError("LOCAL_REGISTRY_COMMAND_FAILED") from exc
    if completed.returncode != 0:
        raise RegistryError("LOCAL_REGISTRY_COMMAND_FAILED")
    return completed.stdout


def wait_for_registry(endpoint: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    url = f"http://{endpoint}/v2/"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RegistryError("LOCAL_REGISTRY_NOT_READY")


def published_digest(target: str) -> str:
    endpoint, remainder = target.split("/", 1)
    repository, reference = remainder.rsplit(":", 1)
    request = urllib.request.Request(
        f"http://{endpoint}/v2/{repository}/manifests/{reference}",
        method="HEAD",
        headers={
            "Accept": (
                "application/vnd.oci.image.index.v1+json, "
                "application/vnd.oci.image.manifest.v1+json, "
                "application/vnd.docker.distribution.manifest.list.v2+json, "
                "application/vnd.docker.distribution.manifest.v2+json"
            )
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            digest = response.headers.get("Docker-Content-Digest")
    except OSError as exc:
        raise RegistryError("LOCAL_REGISTRY_MANIFEST_UNAVAILABLE") from exc
    if not digest:
        raise RegistryError("LOCAL_REGISTRY_MANIFEST_DIGEST_MISSING")
    return digest


def publish(config: dict[str, Any]) -> None:
    endpoint = config["endpoint"]
    compose_file = Path(config["compose_file"])
    compose = ["docker", "compose", "-f", str(compose_file)]
    run(["systemctl", "enable", "--now", "docker"])
    run(["docker", "load", "--input", config["registry_archive"]])
    run([*compose, "up", "-d", "registry"])
    wait_for_registry(endpoint, config["wait_seconds"])
    for image in config["images"]:
        run(["docker", "load", "--input", image["archive"]])
        if image["source"] != image["target"]:
            run(["docker", "tag", image["source"], image["target"]])
        run(["docker", "push", image["target"]])
    for image in config["pinned_images"]:
        expected = image["reference"].split("@", 1)[1]
        if published_digest(image["target"]) != expected:
            raise RegistryError("LOCAL_REGISTRY_PINNED_DIGEST_MISMATCH")
    for image in config["service_images"]:
        run(["docker", "pull", image])
    run([*compose, "up", "-d"])
    running = set(run([*compose, "ps", "--status", "running", "--services"]).split())
    if not {"registry", "vbmc", "sushy-tools"}.issubset(running):
        raise RegistryError("LOCAL_EPH_SERVICES_NOT_RUNNING")
    run(["systemctl", "restart", "containerd"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        config = json.loads(arguments.config.read_text(encoding="utf-8"))
        publish(config)
    except (OSError, ValueError, KeyError, TypeError, RegistryError) as exc:
        print(str(exc) or "LOCAL_REGISTRY_FAILED", file=sys.stderr)
        return 1
    print(json.dumps({"registry": config["endpoint"], "status": "ready"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
