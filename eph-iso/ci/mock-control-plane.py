import argparse
import json
import pathlib
import re
import ssl
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ControlPlaneHandler(BaseHTTPRequestHandler):
    server_version = "EPH-CI-Control-Plane"

    def _send(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _payload(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > 262144:
            raise ValueError("REQUEST_BODY_INVALID")
        return json.loads(self.rfile.read(length))

    def _authorized(self):
        is_enrollment = self.path.endswith("/certificate/")
        token = self.server.enrollment_token if is_enrollment else self.server.agent_token
        return self.headers.get("Authorization") == "Bearer " + token

    def _issue_certificate(self, csr_pem):
        if not isinstance(csr_pem, str) or not csr_pem.startswith("-----BEGIN CERTIFICATE REQUEST-----"):
            raise ValueError("CSR_INVALID")
        with tempfile.TemporaryDirectory(prefix="eph-ci-issuer-") as directory:
            root = pathlib.Path(directory)
            csr_path = root / "agent.csr"
            cert_path = root / "agent.crt"
            ext_path = root / "agent.ext"
            csr_path.write_text(csr_pem, encoding="ascii")
            ext_path.write_text(
                "[client]\n"
                "basicConstraints=critical,CA:FALSE\n"
                "keyUsage=critical,digitalSignature,keyEncipherment\n"
                "extendedKeyUsage=clientAuth\n"
                "subjectKeyIdentifier=hash\n"
                "authorityKeyIdentifier=keyid,issuer\n",
                encoding="ascii",
            )
            subprocess.run(
                [
                    "openssl", "x509", "-req", "-in", str(csr_path),
                    "-CA", self.server.agent_ca, "-CAkey", self.server.agent_ca_key,
                    "-CAcreateserial", "-out", str(cert_path), "-days", "1",
                    "-sha256", "-extfile", str(ext_path), "-extensions", "client",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            certificate = cert_path.read_text(encoding="ascii")
            fingerprint_output = subprocess.run(
                ["openssl", "x509", "-in", str(cert_path), "-noout", "-fingerprint", "-sha256"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            fingerprint = fingerprint_output.split("=", 1)[-1].replace(":", "").lower()
        return certificate, pathlib.Path(self.server.agent_ca).read_text(encoding="ascii"), fingerprint

    def do_POST(self):
        if not self._authorized():
            self._send(401, {"detail": "unauthorized"})
            return
        try:
            payload = self._payload()
            if re.fullmatch(r"/api/v1/agent-runtime/nodes/[0-9a-f-]{36}/certificate/", self.path):
                certificate, chain, fingerprint = self._issue_certificate(payload.get("csr_pem"))
                print("EPH_CI_AGENT_CERTIFICATE_ISSUED", flush=True)
                self._send(
                    200,
                    {
                        "node_id": self.path.split("/")[-3],
                        "client_certificate": certificate,
                        "ca_chain": chain,
                        "fingerprint_sha256": fingerprint,
                        "agent_token": self.server.agent_token,
                        "token_consumed": True,
                    },
                )
                return
            if not self.connection.getpeercert():
                self._send(401, {"detail": "client_certificate_required"})
                return
            if self.path == "/api/v1/agent-runtime/heartbeat/":
                print("EPH_CI_AGENT_MTLS_HEARTBEAT", flush=True)
                self._send(200, {"active_deployments": []})
                return
            if self.path == "/api/v1/agent-runtime/inventory/":
                print("EPH_CI_AGENT_INVENTORY_ACCEPTED", flush=True)
                self._send(200, {"status": "accepted"})
                return
            if self.path == "/api/v1/agent-runtime/commands/poll/":
                print("EPH_CI_AGENT_COMMAND_POLL", flush=True)
                self._send(200, {"command": None})
                return
            self._send(404, {"detail": "not_found"})
        except (ValueError, OSError, subprocess.CalledProcessError, json.JSONDecodeError):
            self._send(400, {"detail": "invalid_request"})

    def log_message(self, format_string, *args):
        return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18443)
    parser.add_argument("--server-cert", required=True)
    parser.add_argument("--server-key", required=True)
    parser.add_argument("--control-ca", required=True)
    parser.add_argument("--agent-ca", required=True)
    parser.add_argument("--agent-ca-key", required=True)
    parser.add_argument("--enrollment-token", required=True)
    parser.add_argument("--agent-token", required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), ControlPlaneHandler)
    server.enrollment_token = args.enrollment_token
    server.agent_token = args.agent_token
    server.agent_ca = args.agent_ca
    server.agent_ca_key = args.agent_ca_key
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.verify_mode = ssl.CERT_OPTIONAL
    context.load_cert_chain(args.server_cert, args.server_key)
    context.load_verify_locations(cafile=args.agent_ca)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
