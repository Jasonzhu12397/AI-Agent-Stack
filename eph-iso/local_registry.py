#!/usr/bin/env python3
"""Run the EPH registry, BMC, and HTTP infrastructure Pods with pinned images.

All workloads run in the EPH single-node Kubernetes cluster. Images are loaded
into containerd and pushed only to the EPH Pod registry; no Docker Compose or
external registry is used. Every subprocess uses an argument vector.
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

KUBECONFIG = "/etc/kubernetes/admin.conf"


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


def kubectl(*arguments: str, timeout: int = 300) -> str:
    return run(["kubectl", "--kubeconfig", KUBECONFIG, *arguments], timeout=timeout)


def wait_for_kubernetes(timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if kubectl("get", "--raw=/readyz", timeout=10).strip() == "ok":
                return
        except RegistryError:
            pass
        time.sleep(2)
    raise RegistryError("LOCAL_KUBERNETES_API_NOT_READY")


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


def import_image(archive: str) -> None:
    run(["ctr", "--namespace", "k8s.io", "images", "import", archive])


def tag_image(source: str, target: str) -> None:
    if source != target:
        run(["ctr", "--namespace", "k8s.io", "images", "tag", source, target])


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
    wait_for_kubernetes(config["wait_seconds"])
    import_image(config["registry_archive"])
    kubectl("apply", "-f", config["registry_manifest"])
    kubectl(
        "--namespace",
        config["namespace"],
        "rollout",
        "status",
        "deployment/registry",
        f"--timeout={config['wait_seconds']}s",
    )
    wait_for_registry(config["endpoint"], config["wait_seconds"])
    for image in config["images"]:
        import_image(image["archive"])
        tag_image(image["source"], image["target"])
        run(
            [
                "ctr",
                "--namespace",
                "k8s.io",
                "images",
                "push",
                "--plain-http",
                image["target"],
            ]
        )
    for image in config["pinned_images"]:
        expected = image["reference"].split("@", 1)[1]
        if published_digest(image["target"]) != expected:
            raise RegistryError("LOCAL_REGISTRY_PINNED_DIGEST_MISMATCH")
    kubectl("apply", "-f", config["services_manifest"])
    for name in config["service_names"]:
        kubectl(
            "--namespace",
            config["namespace"],
            "rollout",
            "status",
            f"deployment/{name}",
            f"--timeout={config['wait_seconds']}s",
        )


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
