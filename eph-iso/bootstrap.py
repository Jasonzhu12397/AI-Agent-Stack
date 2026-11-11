import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import uuid
from pathlib import Path


MEDIA_ROOT = Path("/run/eph-bootstrap-media")
MANIFEST_ROOT = Path("/usr/share/ai-agent-studio/eph-manifests")
IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9._/:@-]+@sha256:[a-f0-9]{64}$")


class BootstrapError(Exception):
    pass


def validate_config(config):
    if not isinstance(config, dict):
        raise BootstrapError("BOOTSTRAP_CONFIG_INVALID")
    required = {"control_url", "node_id", "enrollment_token", "agent_image"}
    if not required.issubset(config):
        raise BootstrapError("BOOTSTRAP_CONFIG_FIELDS_MISSING")
    control_url = config["control_url"]
    if not isinstance(control_url, str):
        raise BootstrapError("CONTROL_URL_INVALID")
    try:
        parsed = urllib.parse.urlsplit(control_url)
        parsed_port = parsed.port
    except ValueError:
        raise BootstrapError("CONTROL_URL_INVALID")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or (parsed_port is not None and not 1 <= parsed_port <= 65535)
    ):
        raise BootstrapError("CONTROL_URL_INVALID")
    try:
        uuid.UUID(config["node_id"])
    except (ValueError, TypeError, AttributeError):
        raise BootstrapError("NODE_ID_INVALID")
    token = config["enrollment_token"]
    if not isinstance(token, str) or not 32 <= len(token) <= 512:
        raise BootstrapError("ENROLLMENT_TOKEN_INVALID")
    if any(ord(character) < 33 or ord(character) > 126 for character in token):
        raise BootstrapError("ENROLLMENT_TOKEN_INVALID")
    image = config["agent_image"]
    if not isinstance(image, str) or not IMAGE_PATTERN.fullmatch(image):
        raise BootstrapError("AGENT_IMAGE_NOT_PINNED_BY_DIGEST")
    return config


def _run(arguments, input_text=None):
    subprocess.run(
        arguments,
        input=input_text,
        text=True,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _copy_secret_file(source, destination):
    source_path = MEDIA_ROOT / source
    if not source_path.is_file() or source_path.is_symlink():
        raise BootstrapError("BOOTSTRAP_SECRET_FILE_MISSING:" + source)
    if source_path.stat().st_size == 0:
        raise BootstrapError("BOOTSTRAP_SECRET_FILE_EMPTY:" + source)
    shutil.copyfile(str(source_path), str(destination))
    os.chmod(str(destination), 0o600)


def _create_secret(name, namespace, files):
    command = [
        "k3s", "kubectl", "--kubeconfig", "/etc/rancher/k3s/k3s.yaml",
        "--namespace", namespace, "create", "secret", "generic", name,
    ]
    for key, path in files.items():
        command.append("--from-file=" + key + "=" + str(path))
    command.extend(["--dry-run=client", "-o", "yaml"])
    rendered = subprocess.run(
        command, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ).stdout
    _run([
        "k3s", "kubectl", "--kubeconfig", "/etc/rancher/k3s/k3s.yaml",
        "apply", "-f", "-"
    ], rendered)


def _apply_file(path, image=None):
    content = path.read_text(encoding="utf-8")
    if image:
        content = content.replace(
            "REPLACE_WITH_REGISTRY/ai-agent-node-agent:VERSION", image
        )
    _run([
        "k3s", "kubectl", "--kubeconfig", "/etc/rancher/k3s/k3s.yaml",
        "apply", "-f", "-"
    ], content)


def bootstrap():
    config_path = MEDIA_ROOT / "bootstrap.json"
    if not config_path.is_file() or config_path.is_symlink():
        raise BootstrapError("BOOTSTRAP_CONFIG_NOT_FOUND")
    config = validate_config(json.loads(config_path.read_text(encoding="utf-8")))

    with tempfile.TemporaryDirectory(prefix="eph-bootstrap-") as temporary:
        secret_dir = Path(temporary)
        credential_files = {}
        for key, value in (
            ("control-url", config["control_url"]),
            ("node-id", config["node_id"]),
            ("token", config["enrollment_token"]),
        ):
            path = secret_dir / key
            path.write_text(value, encoding="utf-8")
            os.chmod(str(path), 0o600)
            credential_files[key] = path

        tls_files = {}
        for key, source in (
            ("ca.crt", "tls/ca.crt"),
            ("tls.crt", "tls/tls.crt"),
            ("tls.key", "tls/tls.key"),
        ):
            path = secret_dir / key
            _copy_secret_file(source, path)
            tls_files[key] = path

        capi_files = {}
        capi_sources = (
            ("source.kubeconfig", "capi/source.kubeconfig"),
            ("target.kubeconfig", "capi/target.kubeconfig"),
        )
        capi_present = [(MEDIA_ROOT / source).exists() for _, source in capi_sources]
        if any(capi_present) and not all(capi_present):
            raise BootstrapError("CAPI_PIVOT_KUBECONFIG_SET_INCOMPLETE")
        if all(capi_present):
            for key, source in capi_sources:
                path = secret_dir / key
                _copy_secret_file(source, path)
                capi_files[key] = path

        _run(["k3s", "kubectl", "--kubeconfig", "/etc/rancher/k3s/k3s.yaml",
              "apply", "-f", str(MANIFEST_ROOT / "namespace.yaml")])
        _run(["k3s", "kubectl", "--kubeconfig", "/etc/rancher/k3s/k3s.yaml",
              "apply", "-f", str(MANIFEST_ROOT / "rbac.yaml")])
        _create_secret("eph-node-agent-credentials", "ai-agent-system", credential_files)
        _create_secret("agent-control-plane-mtls", "ai-agent-system", tls_files)
        if capi_files:
            _create_secret("capi-pivot-kubeconfigs", "ai-agent-system", capi_files)
        _apply_file(MANIFEST_ROOT / "eph-node-agent.yaml", config["agent_image"])
        _run([
            "k3s", "kubectl", "--kubeconfig", "/etc/rancher/k3s/k3s.yaml",
            "--namespace", "ai-agent-system", "rollout", "status",
            "deployment/eph-node-agent", "--timeout=10m"
        ])


if __name__ == "__main__":
    try:
        bootstrap()
    except (BootstrapError, OSError, ValueError, subprocess.CalledProcessError) as error:
        message = str(error) if isinstance(error, BootstrapError) else "EPH_BOOTSTRAP_FAILED"
        print(message, file=__import__("sys").stderr)
        raise SystemExit(1)
