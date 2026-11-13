import base64
import hmac
import json
import os
import pathlib
import re
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

MEDIA_ROOT = pathlib.Path("/etc/ai-agent-studio/eph-bootstrap")
MANIFEST_ROOT = pathlib.Path("/usr/share/ai-agent-studio/eph-manifests")
IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9._/:@-]+@sha256:[a-f0-9]{64}$")
TOKEN_PATTERN = re.compile(r"^[!-~]{32,512}$")
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class BootstrapError(Exception):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def validate_config(config):
    if not isinstance(config, dict):
        raise BootstrapError("BOOTSTRAP_CONFIG_INVALID")
    required = {"control_url", "node_id", "enrollment_token", "agent_image"}
    if not required.issubset(config):
        raise BootstrapError("BOOTSTRAP_CONFIG_FIELDS_MISSING")
    control_url = config["control_url"]
    if not isinstance(control_url, str):
        raise BootstrapError("CONTROL_URL_INVALID") from None
    try:
        parsed = urllib.parse.urlsplit(control_url)
        parsed_port = parsed.port
    except ValueError:
        raise BootstrapError("CONTROL_URL_INVALID") from None
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
    node_id = config["node_id"]
    if not isinstance(node_id, str):
        raise BootstrapError("NODE_ID_INVALID") from None
    try:
        normalized_node_id = str(uuid.UUID(node_id))
    except (ValueError, TypeError, AttributeError):
        raise BootstrapError("NODE_ID_INVALID") from None
    if normalized_node_id != node_id.lower():
        raise BootstrapError("NODE_ID_INVALID")
    token = config["enrollment_token"]
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise BootstrapError("ENROLLMENT_TOKEN_INVALID")
    image = config["agent_image"]
    if not isinstance(image, str) or not IMAGE_PATTERN.fullmatch(image):
        raise BootstrapError("AGENT_IMAGE_NOT_PINNED_BY_DIGEST")
    return config


def _is_pem_certificate_bundle(value):
    if not isinstance(value, str) or not value.strip():
        return False
    pattern = re.compile(
        r"-----BEGIN CERTIFICATE-----\s*([A-Za-z0-9+/=\s]+?)\s*-----END CERTIFICATE-----"
    )
    matches = list(pattern.finditer(value))
    if not matches:
        return False
    remainder = pattern.sub("", value)
    if remainder.strip():
        return False
    try:
        return all(
            base64.b64decode(
                re.sub(r"\s+", "", match.group(1)), validate=True
            )
            for match in matches
        )
    except (ValueError, base64.binascii.Error):
        return False


def _validate_certificate_exchange_response(result, node_id):
    if not isinstance(result, dict):
        raise BootstrapError("CLIENT_CERTIFICATE_RESPONSE_INVALID")
    agent_token = result.get("agent_token")
    certificate = result.get("client_certificate")
    ca_chain = result.get("ca_chain")
    fingerprint = result.get("fingerprint_sha256")
    if (
        not isinstance(agent_token, str)
        or not TOKEN_PATTERN.fullmatch(agent_token)
        or not _is_pem_certificate_bundle(certificate)
        or not _is_pem_certificate_bundle(ca_chain)
        or not isinstance(fingerprint, str)
        or not FINGERPRINT_PATTERN.fullmatch(fingerprint)
        or result.get("node_id") != node_id
        or result.get("token_consumed") is not True
    ):
        raise BootstrapError("CLIENT_CERTIFICATE_RESPONSE_INVALID")
    return agent_token, certificate, ca_chain, fingerprint


def _run(arguments, input_text=None):
    subprocess.run(
        arguments,
        input=input_text,
        text=True,
        check=True,
        capture_output=True,
    )


def _run_output(arguments, timeout=None):
    return subprocess.run(
        arguments,
        text=True,
        check=True,
        capture_output=True,
        timeout=timeout,
    ).stdout


def _copy_secret_file(source, destination):
    source_path = MEDIA_ROOT / source
    try:
        resolved_path = source_path.resolve(strict=True)
    except FileNotFoundError:
        raise BootstrapError("BOOTSTRAP_SECRET_FILE_MISSING:" + source) from None
    if (
        source_path.is_symlink()
        or not resolved_path.is_relative_to(MEDIA_ROOT.resolve())
        or not resolved_path.is_file()
    ):
        raise BootstrapError("BOOTSTRAP_SECRET_FILE_INVALID:" + source)
    if resolved_path.stat().st_size == 0:
        raise BootstrapError("BOOTSTRAP_SECRET_FILE_EMPTY:" + source)
    shutil.copyfile(str(resolved_path), str(destination))
    os.chmod(str(destination), 0o600)


def _request_client_certificate(config, secret_dir, control_ca_path, issuer_ca_path):
    key_path = secret_dir / "tls.key"
    csr_path = secret_dir / "tls.csr"
    certificate_path = secret_dir / "tls.crt"
    common_name = "agent-node-{}.agents.internal".format(config["node_id"])

    try:
        _run([
            "openssl",
            "genpkey",
            "-algorithm",
            "EC",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-out",
            str(key_path),
        ])
        os.chmod(str(key_path), 0o600)
        _run([
            "openssl",
            "req",
            "-new",
            "-key",
            str(key_path),
            "-subj",
            "/CN=" + common_name,
            "-out",
            str(csr_path),
        ])
        os.chmod(str(csr_path), 0o600)
    except subprocess.CalledProcessError:
        raise BootstrapError("LOCAL_CLIENT_KEY_GENERATION_FAILED") from None

    csr_pem = csr_path.read_text(encoding="ascii")
    request_body = json.dumps({"csr_pem": csr_pem}).encode("utf-8")
    endpoint = (
        config["control_url"].rstrip("/")
        + "/api/v1/agent-runtime/nodes/"
        + config["node_id"]
        + "/certificate/"
    )
    request = urllib.request.Request(
        endpoint,
        data=request_body,
        headers={
            "Authorization": "Bearer " + config["enrollment_token"],
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    context = ssl.create_default_context(cafile=str(control_ca_path))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        _NoRedirectHandler(),
    )
    try:
        with opener.open(request, timeout=20) as response:
            body = response.read(262145)
    except urllib.error.HTTPError as error:
        raise BootstrapError(
            f"CLIENT_CERTIFICATE_EXCHANGE_HTTP_STATUS_{error.code}"
        ) from None
    except (urllib.error.URLError, TimeoutError, ssl.SSLError) as error:
        reason = getattr(error, "reason", None)
        reason_name = type(reason).__name__ if reason is not None else type(error).__name__
        raise BootstrapError(
            "CLIENT_CERTIFICATE_EXCHANGE_TRANSPORT_FAILED:" + reason_name
        ) from None
    if len(body) > 262144:
        raise BootstrapError("CLIENT_CERTIFICATE_RESPONSE_TOO_LARGE")
    try:
        result = json.loads(body)
    except (TypeError, ValueError, UnicodeDecodeError):
        raise BootstrapError("CLIENT_CERTIFICATE_RESPONSE_INVALID") from None

    agent_token, certificate, ca_chain, fingerprint = (
        _validate_certificate_exchange_response(result, config["node_id"])
    )

    certificate_path.write_text(certificate + ca_chain, encoding="ascii")
    os.chmod(str(certificate_path), 0o600)
    leaf_path = secret_dir / "tls-leaf.crt"
    chain_path = secret_dir / "tls-chain.crt"
    leaf_path.write_text(certificate, encoding="ascii")
    chain_path.write_text(ca_chain, encoding="ascii")
    validation_stage = "chain"
    try:
        _run([
            "openssl",
            "verify",
            "-x509_strict",
            "-purpose",
            "sslclient",
            "-verify_depth",
            "8",
            "-CAfile",
            str(issuer_ca_path),
            "-untrusted",
            str(chain_path),
            str(leaf_path),
        ])
        validation_stage = "subject"
        subject = _run_output([
            "openssl",
            "x509",
            "-in",
            str(leaf_path),
            "-noout",
            "-subject",
            "-nameopt",
            "RFC2253",
        ]).strip()
        if not hmac.compare_digest(subject, "subject=CN=" + common_name):
            raise BootstrapError("CLIENT_CERTIFICATE_IDENTITY_INVALID")
        validation_stage = "fingerprint"
        actual_fingerprint = _run_output([
            "openssl",
            "x509",
            "-in",
            str(certificate_path),
            "-noout",
            "-fingerprint",
            "-sha256",
        ]).strip().split("=", 1)[-1].replace(":", "").lower()
        validation_stage = "certificate-public-key"
        certificate_public_key = _run_output([
            "openssl",
            "x509",
            "-in",
            str(certificate_path),
            "-pubkey",
            "-noout",
        ]).strip()
        validation_stage = "private-public-key"
        private_public_key = _run_output([
            "openssl",
            "pkey",
            "-in",
            str(key_path),
            "-pubout",
        ]).strip()
    except subprocess.CalledProcessError as error:
        raise BootstrapError(
            "CLIENT_CERTIFICATE_VALIDATION_FAILED:"
            + validation_stage
            + ":"
            + str(error.returncode)
        ) from None
    if (
        not hmac.compare_digest(actual_fingerprint, fingerprint)
        or not hmac.compare_digest(certificate_public_key, private_public_key)
    ):
        raise BootstrapError("CLIENT_CERTIFICATE_KEY_MISMATCH")

    try:
        csr_path.unlink()
    except FileNotFoundError:
        pass
    return {
        "agent_token": agent_token,
        "ca.crt": control_ca_path,
        "tls.crt": certificate_path,
        "tls.key": key_path,
    }


def _wait_for_kubernetes_api():
    command = [
        "kubectl",
        "--kubeconfig",
        "/etc/kubernetes/admin.conf",
        "get",
        "--raw=/readyz",
    ]
    for attempt in range(60):
        try:
            if _run_output(command, timeout=10).strip() == "ok":
                return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        if attempt < 59:
            time.sleep(5)
    raise BootstrapError("KUBERNETES_API_NOT_READY")


def _create_secret(name, namespace, files):
    command = [
        "kubectl", "--kubeconfig", "/etc/kubernetes/admin.conf",
        "--namespace", namespace, "create", "secret", "generic", name,
    ]
    for key, path in files.items():
        command.append("--from-file=" + key + "=" + str(path))
    command.extend(["--dry-run=client", "-o", "yaml"])
    rendered = subprocess.run(
        command, check=True, text=True, capture_output=True
    ).stdout
    _run([
        "kubectl", "--kubeconfig", "/etc/kubernetes/admin.conf",
        "apply", "-f", "-"
    ], rendered)


def _read_secret_files(name, namespace, keys, directory):
    command = [
        "kubectl", "--kubeconfig", "/etc/kubernetes/admin.conf",
        "--namespace", namespace, "get", "secret", name, "-o", "json",
    ]
    result = subprocess.run(command, check=False, text=True, capture_output=True)
    if result.returncode:
        if re.search(r"\(NotFound\)", result.stderr):
            return None
        raise BootstrapError("KUBERNETES_SECRET_READ_FAILED:" + name)
    try:
        payload = json.loads(result.stdout)
        data = payload["data"]
        if not isinstance(data, dict):
            raise ValueError("Secret data is invalid.")
        files = {}
        for key in keys:
            encoded = data.get(key)
            if not isinstance(encoded, str):
                raise ValueError("Secret key is missing.")
            content = base64.b64decode(encoded, validate=True)
            if not content or len(content) > 1048576:
                raise ValueError("Secret value is invalid.")
            path = directory / (name + "-" + key.replace("/", "_"))
            path.write_bytes(content)
            os.chmod(str(path), 0o600)
            files[key] = path
        return files
    except (KeyError, TypeError, ValueError, base64.binascii.Error):
        raise BootstrapError("KUBERNETES_SECRET_DATA_INVALID:" + name) from None


def _validate_persisted_client_credentials(files, issuer_ca_path, directory, node_id):
    certificate_data = files["tls.crt"].read_text(encoding="ascii")
    blocks = re.findall(
        r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
        certificate_data,
        re.DOTALL,
    )
    if len(blocks) < 1 or "".join(blocks).strip() != certificate_data.strip():
        raise BootstrapError("PERSISTED_CLIENT_CERTIFICATE_INVALID")
    leaf_path = directory / "persisted-client-leaf.crt"
    chain_path = directory / "persisted-client-chain.crt"
    leaf_path.write_text(blocks[0] + "\n", encoding="ascii")
    chain_path.write_text("".join(block + "\n" for block in blocks[1:]), encoding="ascii")
    expected_subject = "subject=CN=agent-node-" + node_id + ".agents.internal"
    try:
        _run([
            "openssl", "verify", "-x509_strict", "-purpose", "sslclient",
            "-verify_depth", "8", "-CAfile", str(issuer_ca_path),
            "-untrusted", str(chain_path), str(leaf_path),
        ])
        subject = _run_output([
            "openssl", "x509", "-in", str(leaf_path), "-noout",
            "-subject", "-nameopt", "RFC2253",
        ]).strip()
        certificate_public_key = _run_output([
            "openssl", "x509", "-in", str(leaf_path), "-pubkey", "-noout",
        ]).strip()
        private_public_key = _run_output([
            "openssl", "pkey", "-in", str(files["tls.key"]), "-pubout",
        ]).strip()
    except subprocess.CalledProcessError:
        raise BootstrapError("PERSISTED_CLIENT_CERTIFICATE_VALIDATION_FAILED") from None
    if (
        not hmac.compare_digest(subject, expected_subject)
        or not hmac.compare_digest(certificate_public_key, private_public_key)
    ):
        raise BootstrapError("PERSISTED_CLIENT_CERTIFICATE_IDENTITY_INVALID")


def _apply_file(path, image=None):
    content = path.read_text(encoding="utf-8")
    if image:
        placeholder = "REPLACE_WITH_REGISTRY/ai-agent-node-agent:VERSION"
        if content.count(placeholder) != 1:
            raise BootstrapError("EPH_AGENT_IMAGE_PLACEHOLDER_INVALID")
        content = content.replace(placeholder, image)
    _run([
        "kubectl", "--kubeconfig", "/etc/kubernetes/admin.conf",
        "apply", "-f", "-"
    ], content)


def _wait_for_agent_pod():
    kubectl = [
        "kubectl",
        "--kubeconfig",
        "/etc/kubernetes/admin.conf",
        "--namespace",
        "ai-agent-system",
    ]
    _run(kubectl + [
        "wait",
        "--for=condition=Ready",
        "pod",
        "--selector=app.kubernetes.io/name=eph-node-agent",
        "--timeout=300s",
    ])
    ready_pods = _run_output(kubectl + [
        "get",
        "pods",
        "--selector=app.kubernetes.io/name=eph-node-agent",
        "--field-selector=status.phase=Running",
        "-o",
        "jsonpath={.items[0].metadata.name}",
    ], timeout=15).strip()
    if not ready_pods:
        raise BootstrapError("EPH_AGENT_POD_NOT_READY")


def bootstrap():
    config_path = MEDIA_ROOT / "bootstrap.json"
    if (
        not config_path.is_file()
        or config_path.is_symlink()
        or config_path.stat().st_size > 65536
    ):
        raise BootstrapError("BOOTSTRAP_CONFIG_NOT_FOUND")
    config = validate_config(json.loads(config_path.read_text(encoding="utf-8")))

    with tempfile.TemporaryDirectory(prefix="eph-bootstrap-") as temporary:
        _wait_for_kubernetes_api()
        _run([
            "kubectl",
            "--kubeconfig",
            "/etc/kubernetes/admin.conf",
            "wait",
            "--for=condition=Ready",
            "nodes",
            "--all",
            "--timeout=300s",
        ])
        secret_dir = pathlib.Path(temporary)
        namespace_manifest = MANIFEST_ROOT / "namespace.yaml"
        rbac_manifest = MANIFEST_ROOT / "rbac.yaml"
        _run([
            "kubectl", "--kubeconfig", "/etc/kubernetes/admin.conf",
            "apply", "-f", str(namespace_manifest),
        ])
        _run([
            "kubectl", "--kubeconfig", "/etc/kubernetes/admin.conf",
            "apply", "-f", str(rbac_manifest),
        ])

        credential_files = {}
        for key, value in (
            ("control-url", config["control_url"]),
            ("node-id", config["node_id"]),
        ):
            path = secret_dir / key
            path.write_text(value, encoding="utf-8")
            os.chmod(str(path), 0o600)
            credential_files[key] = path

        control_ca_path = secret_dir / "ca.crt"
        _copy_secret_file("tls/ca.crt", control_ca_path)
        client_ca_path = secret_dir / "client-ca.crt"
        _copy_secret_file("tls/agent-ca.crt", client_ca_path)

        existing_credentials = _read_secret_files(
            "eph-node-agent-credentials", "ai-agent-system", ("token",), secret_dir
        )
        existing_tls = _read_secret_files(
            "agent-control-plane-mtls",
            "ai-agent-system",
            ("ca.crt", "tls.crt", "tls.key"),
            secret_dir,
        )
        if (existing_credentials is None) != (existing_tls is None):
            raise BootstrapError("EPH_AGENT_CREDENTIAL_SECRET_SET_INCOMPLETE")
        if existing_credentials is not None:
            tls_files = existing_tls
            token_path = existing_credentials["token"]
            if not TOKEN_PATTERN.fullmatch(token_path.read_text(encoding="ascii")):
                raise BootstrapError("PERSISTED_AGENT_TOKEN_INVALID")
            _validate_persisted_client_credentials(
                tls_files, client_ca_path, secret_dir, config["node_id"]
            )
        else:
            tls_files = _request_client_certificate(
                config, secret_dir, control_ca_path, client_ca_path
            )
            token_path = secret_dir / "token"
            token_path.write_text(tls_files.pop("agent_token"), encoding="utf-8")
            os.chmod(str(token_path), 0o600)
        credential_files["token"] = token_path

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

        _create_secret("eph-node-agent-credentials", "ai-agent-system", credential_files)
        _create_secret("agent-control-plane-mtls", "ai-agent-system", tls_files)
        if capi_files:
            _create_secret("capi-pivot-kubeconfigs", "ai-agent-system", capi_files)
        _apply_file(MANIFEST_ROOT / "eph-node-agent.yaml", config["agent_image"])
        _run([
            "kubectl", "--kubeconfig", "/etc/kubernetes/admin.conf",
            "--namespace", "ai-agent-system", "rollout", "status",
            "deployment/eph-node-agent", "--timeout=10m"
        ])
        _wait_for_agent_pod()


if __name__ == "__main__":
    try:
        bootstrap()
    except (BootstrapError, OSError, ValueError, subprocess.CalledProcessError) as error:
        message = str(error) if isinstance(error, BootstrapError) else "EPH_BOOTSTRAP_FAILED"
        print(message, file=__import__("sys").stderr)
        raise SystemExit(1) from None
