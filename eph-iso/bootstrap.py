"""EPH Live ISO bootstrap: apply the rendered EPH Agent onto the local cluster.

This script runs inside the Live ISO after ``kubeadm init``. It no longer
performs any certificate exchange: the Cloud Control Plane issues the Agent
client certificate out of band and the per-node NoCloud seed simply carries the
material. Everything the script validates is therefore already on the node:

* ``bootstrap.json`` (control URL, Agent identity, pinned Agent image),
* ``tls/ca.crt`` -- trust anchor for the Cloud Control Plane endpoint,
* ``tls/tls.crt`` / ``tls/tls.key`` -- the issued Agent client certificate,
* ``tls/agent-ca.crt`` -- issuer of the client certificate, used only to
  verify the leaf locally before it is mounted into the cluster.

The certificate layout, secret name and Kubernetes object names match
``apps/eph_agent/bootstrap_renderer.py`` and
``packages/agent_transport/config.py``; the Agent itself reads its identity and
TLS paths from the environment rendered into the Deployment.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit

MEDIA_ROOT = pathlib.Path("/etc/ai-agent-studio/eph-bootstrap")
MANIFEST_ROOT = pathlib.Path("/usr/share/ai-agent-studio/eph-manifests")
IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9._/:@-]+@sha256:[a-f0-9]{64}$")
SECRET_NAME = "eph-agent-mtls"
NAMESPACE = "ai-agent-system"
DEPLOYMENT_NAME = "eph-agent"
IMAGE_PLACEHOLDER = "REPLACE_WITH_REGISTRY/ai-agent-node-agent:VERSION"
MAX_CONFIG_BYTES = 65536

# Environment variables consumed by ``AgentSettings.from_environment("eph")``.
# The bootstrap asserts that the rendered Deployment always provides them, so a
# Deployment that would fail at start-up is rejected before it is applied.
REQUIRED_AGENT_ENVIRONMENT = (
    "AGENT_CONTROL_URL",
    "AGENT_ROLE",
    "AGENT_ID",
    "AGENT_INSTALLATION_ID",
    "AGENT_INSTANCE_ID",
    "AGENT_TLS_CA_FILE",
    "AGENT_TLS_CERT_FILE",
    "AGENT_TLS_KEY_FILE",
)

TLS_FILES = (
    ("ca.crt", "tls/ca.crt"),
    ("tls.crt", "tls/tls.crt"),
    ("tls.key", "tls/tls.key"),
)


class BootstrapError(Exception):
    pass


def _valid_uuid(value: object, code: str) -> str:
    if not isinstance(value, str):
        raise BootstrapError(code)
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise BootstrapError(code) from None
    if str(parsed) != value.lower():
        raise BootstrapError(code)
    return str(parsed)


def validate_config(config: object) -> dict:
    if not isinstance(config, dict):
        raise BootstrapError("BOOTSTRAP_CONFIG_INVALID")
    required = {"control_url", "agent_id", "installation_id", "agent_image"}
    if not required.issubset(config):
        raise BootstrapError("BOOTSTRAP_CONFIG_FIELDS_MISSING")
    control_url = config["control_url"]
    if not isinstance(control_url, str):
        raise BootstrapError("CONTROL_URL_INVALID")
    parsed = urlsplit(control_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise BootstrapError("CONTROL_URL_INVALID")
    agent_id = _valid_uuid(config["agent_id"], "AGENT_ID_INVALID")
    installation_id = _valid_uuid(config["installation_id"], "INSTALLATION_ID_INVALID")
    instance_id = config.get("instance_id")
    if instance_id is not None:
        if not isinstance(instance_id, str) or not instance_id or len(instance_id) > 128:
            raise BootstrapError("INSTANCE_ID_INVALID")
    image = config["agent_image"]
    if not isinstance(image, str) or not IMAGE_PATTERN.fullmatch(image):
        raise BootstrapError("AGENT_IMAGE_NOT_PINNED_BY_DIGEST")
    return {
        "control_url": control_url.rstrip("/"),
        "agent_id": agent_id,
        "installation_id": installation_id,
        "instance_id": instance_id,
        "agent_image": image,
    }


def _run(arguments: list[str], input_text: str | None = None) -> None:
    subprocess.run(arguments, input=input_text, text=True, check=True, capture_output=True)


def _run_output(arguments: list[str], timeout: int | None = None) -> str:
    return subprocess.run(
        arguments, text=True, check=True, capture_output=True, timeout=timeout
    ).stdout


def _copy_secret_file(source: str, destination: pathlib.Path) -> pathlib.Path:
    source_path = MEDIA_ROOT / source
    try:
        resolved = source_path.resolve(strict=True)
    except FileNotFoundError:
        raise BootstrapError("BOOTSTRAP_SECRET_FILE_MISSING:" + source) from None
    if (
        source_path.is_symlink()
        or not resolved.is_relative_to(MEDIA_ROOT.resolve())
        or not resolved.is_file()
    ):
        raise BootstrapError("BOOTSTRAP_SECRET_FILE_INVALID:" + source)
    if resolved.stat().st_size == 0:
        raise BootstrapError("BOOTSTRAP_SECRET_FILE_EMPTY:" + source)
    shutil.copyfile(str(resolved), str(destination))
    os.chmod(str(destination), 0o600)
    return destination


def verify_keypair(leaf_path: pathlib.Path, ca_path: pathlib.Path, key_path: pathlib.Path) -> str:
    """Verify the issued client certificate against its private key and issuer.

    Returns the lower-case SHA-256 fingerprint of the certificate. Any openssl
    failure is reported as a BootstrapError instead of leaking command output.
    """

    try:
        _run([
            "openssl", "verify", "-x509_strict", "-purpose", "sslclient",
            "-verify_depth", "8", "-CAfile", str(ca_path), str(leaf_path),
        ])
        fingerprint = _run_output([
            "openssl", "x509", "-in", str(leaf_path), "-noout",
            "-fingerprint", "-sha256",
        ]).strip().split("=", 1)[-1].replace(":", "").lower()
        certificate_public_key = _run_output([
            "openssl", "x509", "-in", str(leaf_path), "-pubkey", "-noout",
        ]).strip()
        private_public_key = _run_output([
            "openssl", "pkey", "-in", str(key_path), "-pubout",
        ]).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        raise BootstrapError("CLIENT_CERTIFICATE_VALIDATION_FAILED") from None
    if not hmac.compare_digest(certificate_public_key, private_public_key):
        raise BootstrapError("CLIENT_CERTIFICATE_KEY_MISMATCH")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise BootstrapError("CLIENT_CERTIFICATE_VALIDATION_FAILED")
    return fingerprint


def validate_deployment_manifest(content: str, image: str) -> str:
    if not isinstance(content, str) or IMAGE_PLACEHOLDER not in content:
        raise BootstrapError("EPH_AGENT_IMAGE_PLACEHOLDER_INVALID")
    if content.count(IMAGE_PLACEHOLDER) != 1:
        raise BootstrapError("EPH_AGENT_IMAGE_PLACEHOLDER_INVALID")
    if IMAGE_PATTERN.fullmatch(image) is None:
        raise BootstrapError("AGENT_IMAGE_NOT_PINNED_BY_DIGEST")
    rendered = content.replace(IMAGE_PLACEHOLDER, image)
    missing = [name for name in REQUIRED_AGENT_ENVIRONMENT if name not in rendered]
    if missing:
        raise BootstrapError(
            "EPH_AGENT_DEPLOYMENT_ENVIRONMENT_INCOMPLETE:" + ",".join(sorted(missing))
        )
    return rendered


def _apply_kubeconfig() -> list[str]:
    return ["kubectl", "--kubeconfig", "/etc/kubernetes/admin.conf"]


def wait_for_kubernetes_api(attempts: int = 60, interval: int = 5) -> None:
    command = _apply_kubeconfig() + ["get", "--raw=/readyz"]
    for attempt in range(attempts):
        try:
            if _run_output(command, timeout=10).strip() == "ok":
                return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        if attempt < attempts - 1:
            time.sleep(interval)
    raise BootstrapError("KUBERNETES_API_NOT_READY")


def create_secret(name: str, files: dict[str, pathlib.Path]) -> None:
    command = _apply_kubeconfig() + [
        "--namespace", NAMESPACE, "create", "secret", "generic", name,
        "--dry-run=client", "-o", "yaml",
    ]
    for key, path in files.items():
        command.append("--from-file=" + key + "=" + str(path))
    rendered = subprocess.run(command, check=True, text=True, capture_output=True).stdout
    _run(_apply_kubeconfig() + ["apply", "-f", "-"], rendered)


def apply_manifest(path: pathlib.Path) -> None:
    _run(_apply_kubeconfig() + ["apply", "-f", str(path)])


def apply_text(manifest: str) -> None:
    _run(_apply_kubeconfig() + ["apply", "-f", "-"], manifest)


def wait_for_agent_pod() -> None:
    kubectl = _apply_kubeconfig() + ["--namespace", NAMESPACE]
    _run(kubectl + [
        "rollout", "status", "deployment/" + DEPLOYMENT_NAME, "--timeout=10m",
    ])
    _run(kubectl + [
        "wait", "--for=condition=Ready", "pod",
        "--selector=app.kubernetes.io/name=" + DEPLOYMENT_NAME, "--timeout=300s",
    ])
    running = _run_output(kubectl + [
        "get", "pods", "--selector=app.kubernetes.io/name=" + DEPLOYMENT_NAME,
        "--field-selector=status.phase=Running", "-o",
        "jsonpath={.items[0].metadata.name}",
    ], timeout=15).strip()
    if not running:
        raise BootstrapError("EPH_AGENT_POD_NOT_READY")


def _load_config() -> dict:
    config_path = MEDIA_ROOT / "bootstrap.json"
    if (
        not config_path.is_file()
        or config_path.is_symlink()
        or config_path.stat().st_size > MAX_CONFIG_BYTES
    ):
        raise BootstrapError("BOOTSTRAP_CONFIG_NOT_FOUND")
    try:
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise BootstrapError("BOOTSTRAP_CONFIG_INVALID") from None
    return validate_config(document)


def bootstrap(media_root: pathlib.Path | None = None) -> dict:
    """Apply the EPH Agent; returns a summary of the verified material."""

    global MEDIA_ROOT
    if media_root is not None:
        MEDIA_ROOT = media_root
    config = _load_config()

    with tempfile.TemporaryDirectory(prefix="eph-bootstrap-") as temporary:
        wait_for_kubernetes_api()
        _run(_apply_kubeconfig() + [
            "wait", "--for=condition=Ready", "nodes", "--all", "--timeout=300s",
        ])

        secret_dir = pathlib.Path(temporary)
        tls_paths: dict[str, pathlib.Path] = {}
        for secret_key, source in TLS_FILES:
            tls_paths[secret_key] = _copy_secret_file(source, secret_dir / secret_key)
        issuer_path = _copy_secret_file("tls/agent-ca.crt", secret_dir / "agent-ca.crt")
        fingerprint = verify_keypair(tls_paths["tls.crt"], issuer_path, tls_paths["tls.key"])

        capi_sources = (
            ("source.kubeconfig", "capi/source.kubeconfig"),
            ("target.kubeconfig", "capi/target.kubeconfig"),
        )
        capi_present = [(MEDIA_ROOT / source).exists() for _, source in capi_sources]
        if any(capi_present) and not all(capi_present):
            raise BootstrapError("CAPI_PIVOT_KUBECONFIG_SET_INCOMPLETE")
        capi_files: dict[str, pathlib.Path] = {}
        if all(capi_present):
            for key, source in capi_sources:
                capi_files[key] = _copy_secret_file(source, secret_dir / key)

        apply_manifest(MANIFEST_ROOT / "namespace.yaml")
        apply_manifest(MANIFEST_ROOT / "rbac.yaml")
        create_secret(SECRET_NAME, tls_paths)
        if capi_files:
            create_secret("capi-pivot-kubeconfigs", capi_files)

        deployment = validate_deployment_manifest(
            (MANIFEST_ROOT / "eph-node-agent.yaml").read_text(encoding="utf-8"),
            config["agent_image"],
        )
        apply_text(deployment)
        wait_for_agent_pod()

    return {
        "agent_id": config["agent_id"],
        "installation_id": config["installation_id"],
        "secret": SECRET_NAME,
        "certificate_fingerprint_sha256": fingerprint,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply the EPH Agent from the NoCloud seed.")
    parser.add_argument("--media-root", default=str(MEDIA_ROOT))
    arguments = parser.parse_args(argv)
    try:
        summary = bootstrap(pathlib.Path(arguments.media_root))
    except (BootstrapError, OSError, ValueError, subprocess.CalledProcessError) as error:
        message = str(error) if isinstance(error, BootstrapError) else "EPH_BOOTSTRAP_FAILED"
        print(message, file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
