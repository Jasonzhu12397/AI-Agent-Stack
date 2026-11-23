"""End-to-end check of the Agent mTLS ingress (HAProxy) in Docker.

Python replacement for the former ``tests/integration/test_agent_mtls_proxy.sh``.
It mints a one-day CA, a server and a client certificate, starts an upstream
echo service and the shipped HAProxy config in containers, then asserts:

* a client with a valid certificate reaches the upstream over TLS;
* the proxy forwards the DER SHA-256 of the client certificate, replacing a
  forged ``X-Verified-Client-Cert-SHA256`` header sent by the client;
* ``X-Forwarded-Proto`` is ``https``;
* a client without a certificate is rejected at the handshake;
* a path outside the agent runtime surface is refused by the proxy.

Requires ``docker`` and ``openssl`` on PATH.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
HEARTBEAT_PATH = "/api/v1/agent-runtime/heartbeat"


def run(cmd: list[str], *, capture: bool = False, check: bool = True) -> str:
    result = subprocess.run(cmd, text=True, capture_output=True)
    if check and result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    return result.stdout if capture else ""


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


UPSTREAM_SCRIPT = """import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "path": self.path,
            "fingerprint": self.headers.get("X-Verified-Client-Cert-SHA256"),
            "forwarded_proto": self.headers.get("X-Forwarded-Proto"),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
"""


def openssl(*args: str) -> None:
    run(["openssl", *args])


def mint_certificates(directory: Path) -> None:
    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(directory / "ca.key"), "-out", str(directory / "ca.crt"),
            "-subj", "/CN=Agent mTLS Integration CA", "-days", "1")

    openssl("req", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(directory / "server.key"),
            "-out", str(directory / "server.csr"), "-subj", "/CN=localhost")
    (directory / "server.ext").write_text(
        "subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n",
        encoding="utf-8",
    )
    openssl("x509", "-req", "-in", str(directory / "server.csr"),
            "-CA", str(directory / "ca.crt"), "-CAkey", str(directory / "ca.key"),
            "-CAcreateserial", "-out", str(directory / "server.crt"),
            "-days", "1", "-extfile", str(directory / "server.ext"))
    (directory / "agent-ingress.pem").write_bytes(
        (directory / "server.crt").read_bytes() + (directory / "server.key").read_bytes()
    )

    openssl("req", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(directory / "client.key"),
            "-out", str(directory / "client.csr"), "-subj", "/CN=agent-node-ci")
    (directory / "client.ext").write_text(
        "extendedKeyUsage=clientAuth\n", encoding="utf-8"
    )
    openssl("x509", "-req", "-in", str(directory / "client.csr"),
            "-CA", str(directory / "ca.crt"), "-CAkey", str(directory / "ca.key"),
            "-CAcreateserial", "-out", str(directory / "client.crt"),
            "-days", "1", "-extfile", str(directory / "client.ext"))


def request(
    port: int,
    directory: Path,
    path: str,
    *,
    client_cert: bool = True,
    forged_fingerprint: bool = False,
) -> tuple[int, dict | str]:
    context = ssl.create_default_context(cafile=str(directory / "ca.crt"))
    if client_cert:
        context.load_cert_chain(
            str(directory / "client.crt"), str(directory / "client.key")
        )
    connection = http.client.HTTPSConnection(
        "localhost", port, context=context, timeout=10
    )
    headers = {}
    if forged_fingerprint:
        headers["X-Verified-Client-Cert-SHA256"] = "forged-value"
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read().decode()
        status = response.status
    finally:
        connection.close()
    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        return status, body


def fingerprint(certificate: Path) -> str:
    # DER is binary, so it is captured as bytes rather than decoded text.
    result = subprocess.run(
        ["openssl", "x509", "-in", str(certificate), "-outform", "DER"],
        capture_output=True,
        check=True,
    )
    return hashlib.sha256(result.stdout).hexdigest()


def main() -> None:
    for binary, code in (("docker", "DOCKER_NOT_INSTALLED"),
                         ("openssl", "OPENSSL_NOT_INSTALLED")):
        if shutil.which(binary) is None:
            fail(code)

    suffix = uuid.uuid4().hex[:8]
    network = f"agent-mtls-ci-{suffix}"
    upstream = f"agent-mtls-upstream-{suffix}"
    proxy = f"agent-mtls-proxy-{suffix}"
    port = 18000 + int(suffix[:4], 16) % 10000

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        mint_certificates(directory)
        (directory / "upstream.py").write_text(UPSTREAM_SCRIPT, encoding="utf-8")
        try:
            run(["docker", "network", "create", network])
            run([
                "docker", "run", "-d", "--name", upstream,
                "--network", network, "--network-alias", "api",
                "-v", f"{directory / 'upstream.py'}:/tmp/upstream.py:ro",
                "python:3.12-alpine", "python", "/tmp/upstream.py",
            ])
            run([
                "docker", "run", "-d", "--name", proxy, "--network", network,
                "-p", f"127.0.0.1:{port}:8443",
                "-v",
                f"{ROOT_DIR / 'deploy/cloud-control-plane/agent-mtls/haproxy.cfg'}"
                ":/usr/local/etc/haproxy/haproxy.cfg:ro",
                "-v", f"{directory / 'agent-ingress.pem'}:/run/secrets/agent-ingress.pem:ro",
                "-v", f"{directory / 'ca.crt'}:/run/secrets/agent-client-ca.pem:ro",
                "haproxy:3.2-alpine",
            ])

            expected = fingerprint(directory / "client.crt")
            payload: dict | str = ""
            for _ in range(30):
                try:
                    status, payload = request(
                        port, directory, HEARTBEAT_PATH, forged_fingerprint=True
                    )
                except (ssl.SSLError, OSError):
                    time.sleep(1)
                    continue
                if status == 200:
                    break
                time.sleep(1)
            if not payload:
                fail("The mTLS ingress never answered the heartbeat request")
            if not isinstance(payload, dict):
                fail(f"Unexpected heartbeat payload: {payload!r}")
            if payload.get("path") != HEARTBEAT_PATH:
                fail(f"Unexpected forwarded path: {payload}")
            if payload.get("forwarded_proto") != "https":
                fail(f"Forwarded proto was not https: {payload}")
            if payload.get("fingerprint") != expected:
                fail(
                    "The proxy did not forward the client certificate fingerprint: "
                    f"{payload}"
                )

            try:
                status, _ = request(port, directory, HEARTBEAT_PATH, client_cert=False)
            except ssl.SSLError:
                status = 0
            if status == 200:
                fail("The mTLS ingress accepted a client without a certificate.")

            status, _ = request(port, directory, "/api/v1/tenants/")
            if status != 404:
                fail(f"The proxy did not restrict non-agent paths (HTTP {status})")

            print(
                "Agent mTLS proxy integration passed: handshake, DER SHA-256 identity, "
                "spoofed-header stripping, and path restriction."
            )
        finally:
            run(["docker", "rm", "-f", proxy, upstream], check=False)
            run(["docker", "network", "rm", network], check=False)


if __name__ == "__main__":
    main()
