import base64
import datetime
import hashlib
import hmac
import json
import os
import ssl
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, padding, rsa
from cryptography.x509.oid import NameOID


class CertificateRotationError(Exception):
    pass


def _utc_now():
    return datetime.datetime.now(datetime.UTC)


def _certificate_time(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC)


def _load_certificates(pem_data):
    marker = b"-----BEGIN CERTIFICATE-----"
    if not isinstance(pem_data, bytes) or not pem_data or len(pem_data) > 1024 * 1024:
        raise CertificateRotationError("AGENT_TLS_ROTATION_CA_CHAIN_INVALID")
    blocks = pem_data.split(marker)
    if blocks[0].strip():
        raise CertificateRotationError("AGENT_TLS_ROTATION_CA_CHAIN_INVALID")
    certificates = []
    for block in blocks[1:]:
        encoded = marker + block
        end = encoded.find(b"-----END CERTIFICATE-----")
        if end < 0:
            raise CertificateRotationError("AGENT_TLS_ROTATION_CA_CHAIN_INVALID")
        certificate_pem = encoded[:end + len(b"-----END CERTIFICATE-----")] + b"\n"
        try:
            certificates.append(x509.load_pem_x509_certificate(certificate_pem))
        except ValueError as exc:
            raise CertificateRotationError("AGENT_TLS_ROTATION_CA_CHAIN_INVALID") from exc
        remainder = encoded[end + len(b"-----END CERTIFICATE-----"):]
        if remainder.strip():
            raise CertificateRotationError("AGENT_TLS_ROTATION_CA_CHAIN_INVALID")
    if not certificates or len(certificates) > 32:
        raise CertificateRotationError("AGENT_TLS_ROTATION_CA_CHAIN_INVALID")
    return certificates


def _verify_signature(certificate, issuer):
    public_key = issuer.public_key()
    try:
        if isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(
                certificate.signature,
                certificate.tbs_certificate_bytes,
                padding.PKCS1v15(),
                certificate.signature_hash_algorithm,
            )
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(
                certificate.signature,
                certificate.tbs_certificate_bytes,
                ec.ECDSA(certificate.signature_hash_algorithm),
            )
        elif isinstance(public_key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
            public_key.verify(certificate.signature, certificate.tbs_certificate_bytes)
        else:
            raise CertificateRotationError("AGENT_TLS_ROTATION_SIGNATURE_ALGORITHM_UNSUPPORTED")
    except Exception as exc:
        if isinstance(exc, CertificateRotationError):
            raise
        raise CertificateRotationError("AGENT_TLS_ROTATION_CERTIFICATE_SIGNATURE_INVALID") from exc


def validate_issued_certificate(
    certificate_pem,
    ca_chain_pem,
    private_key,
    node_id,
    trusted_certificate_bundle,
):
    try:
        certificate = x509.load_pem_x509_certificate(certificate_pem.encode("ascii"))
        common_names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        expected_name = f"agent-node-{node_id}.agents.internal"
        if len(common_names) != 1 or common_names[0].value != expected_name:
            raise CertificateRotationError("AGENT_TLS_ROTATION_CERTIFICATE_IDENTITY_INVALID")
        if certificate.public_key().public_numbers() != private_key.public_key().public_numbers():
            raise CertificateRotationError("AGENT_TLS_ROTATION_PRIVATE_KEY_MISMATCH")
        now = _utc_now()
        if _certificate_time(certificate.not_valid_before) > now:
            raise CertificateRotationError("AGENT_TLS_ROTATION_CERTIFICATE_NOT_YET_VALID")
        if _certificate_time(certificate.not_valid_after) <= now:
            raise CertificateRotationError("AGENT_TLS_ROTATION_CERTIFICATE_EXPIRED")
        chain = _load_certificates(ca_chain_pem.encode("ascii"))
        trust_bundle = _load_certificates(trusted_certificate_bundle)
    except (OSError, UnicodeError, ValueError, AttributeError) as exc:
        if isinstance(exc, CertificateRotationError):
            raise
        raise CertificateRotationError("AGENT_TLS_ROTATION_CERTIFICATE_INVALID") from exc
    issuer = chain[0]
    if certificate.issuer != issuer.subject:
        raise CertificateRotationError("AGENT_TLS_ROTATION_ISSUER_MISMATCH")
    _verify_signature(certificate, issuer)
    for index in range(len(chain) - 1):
        current = chain[index]
        parent = chain[index + 1]
        if current.issuer != parent.subject:
            raise CertificateRotationError("AGENT_TLS_ROTATION_CA_CHAIN_INVALID")
        _verify_signature(current, parent)
    last = chain[-1]
    trusted = {
        item.fingerprint(hashes.SHA256())
        for item in trust_bundle
    }
    if last.fingerprint(hashes.SHA256()) not in trusted:
        raise CertificateRotationError("AGENT_TLS_ROTATION_UNTRUSTED_CA")
    return hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()


class CertificateRotationManager:
    def __init__(self, client):
        self.client = client
        self.enabled = os.environ.get("AGENT_TLS_ROTATION_ENABLED", "false").lower() in {
            "1", "true", "yes", "on"
        }
        self.cert_file = os.environ.get("AGENT_TLS_CERT_FILE", "").strip()
        self.key_file = os.environ.get("AGENT_TLS_KEY_FILE", "").strip()
        self.ca_file = os.environ.get("AGENT_TLS_CA_FILE", "").strip()
        self.secret_name = os.environ.get(
            "AGENT_TLS_ROTATION_SECRET_NAME",
            "agent-control-plane-mtls",
        )
        self.namespace = os.environ.get("POD_NAMESPACE", "ai-agent-system")
        self.threshold = datetime.timedelta(
            hours=int(os.environ.get("AGENT_TLS_ROTATION_RENEW_BEFORE_HOURS", "6"))
        )
        self.last_check = None
        self.check_interval = datetime.timedelta(seconds=30)
        self.api_timeout = 10

    def _kubernetes_request(self, method, path, payload=None):
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "").strip()
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443").strip()
        token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
        ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
        if not host or not token_path.is_file() or not ca_path.is_file():
            raise CertificateRotationError("AGENT_TLS_ROTATION_KUBERNETES_API_UNAVAILABLE")
        try:
            token = token_path.read_text(encoding="ascii").strip()
            context = ssl.create_default_context(cafile=str(ca_path))
        except (OSError, UnicodeError, ssl.SSLError) as exc:
            raise CertificateRotationError(
                "AGENT_TLS_ROTATION_KUBERNETES_AUTH_UNAVAILABLE"
            ) from exc
        if not token or len(token) > 16384:
            raise CertificateRotationError("AGENT_TLS_ROTATION_KUBERNETES_AUTH_INVALID")
        request = urllib.request.Request(
            f"https://{host}:{port}{path}",
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/merge-patch+json",
            },
            method=method,
        )
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=context),
            )
            with opener.open(request, timeout=self.api_timeout) as response:
                body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise CertificateRotationError("AGENT_TLS_ROTATION_KUBERNETES_RESPONSE_TOO_LARGE")
            result = json.loads(body.decode("utf-8"))
            if not isinstance(result, dict):
                raise CertificateRotationError("AGENT_TLS_ROTATION_KUBERNETES_RESPONSE_INVALID")
            return result
        except urllib.error.HTTPError as exc:
            raise CertificateRotationError(
                f"AGENT_TLS_ROTATION_KUBERNETES_HTTP_{exc.code}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, UnicodeError, json.JSONDecodeError) as exc:
            raise CertificateRotationError("AGENT_TLS_ROTATION_KUBERNETES_API_UNAVAILABLE") from exc

    def _secret_path(self):
        return (
            f"/api/v1/namespaces/{self.namespace}/secrets/"
            f"{self.secret_name}"
        )

    def _get_secret(self):
        return self._kubernetes_request("GET", self._secret_path())

    def _patch_secret(self, data):
        return self._kubernetes_request(
            "PATCH",
            self._secret_path(),
            {"data": data},
        )

    @staticmethod
    def _decode_secret_value(data, key):
        encoded = data.get(key)
        if not isinstance(encoded, str):
            return None
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise CertificateRotationError("AGENT_TLS_ROTATION_SECRET_DATA_INVALID") from exc

    @staticmethod
    def _encode_secret_value(value):
        return base64.b64encode(value).decode("ascii")

    def _load_context(self, certificate_pem, private_key_pem):
        with tempfile.TemporaryDirectory(prefix="eph-agent-tls-") as directory:
            cert_path = Path(directory) / "tls.crt"
            key_path = Path(directory) / "tls.key"
            cert_path.write_bytes(certificate_pem)
            key_path.write_bytes(private_key_pem)
            os.chmod(cert_path, 0o600)
            os.chmod(key_path, 0o600)
            context = ssl.create_default_context(cafile=self.ca_file or None)
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_cert_chain(str(cert_path), str(key_path))
            return context

    def _new_key_and_csr(self):
        private_key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([
            x509.NameAttribute(
                NameOID.COMMON_NAME,
                f"agent-node-{self.client.node_id}.agents.internal",
            )
        ])
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(subject)
            .sign(private_key, hashes.SHA256())
        )
        key_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode("ascii")
        return private_key, key_pem, csr_pem

    def _activate_pending(self, data):
        certificate_pem = self._decode_secret_value(data, "pending.tls.crt")
        private_key_pem = self._decode_secret_value(data, "pending.tls.key")
        created_at_raw = self._decode_secret_value(data, "pending.created_at")
        if not certificate_pem or not private_key_pem or not created_at_raw:
            return False
        try:
            created_at = datetime.datetime.fromisoformat(
                created_at_raw.decode("ascii").replace("Z", "+00:00")
            )
        except (UnicodeError, ValueError) as exc:
            raise CertificateRotationError("AGENT_TLS_ROTATION_PENDING_TIMESTAMP_INVALID") from exc
        if created_at.tzinfo is None:
            raise CertificateRotationError("AGENT_TLS_ROTATION_PENDING_TIMESTAMP_INVALID")
        pending_age = _utc_now() - created_at.astimezone(datetime.UTC)
        if pending_age >= datetime.timedelta(minutes=9):
            self._patch_secret({
                "pending.tls.crt": None,
                "pending.tls.key": None,
                "pending.created_at": None,
            })
            return False
        context = self._load_context(certificate_pem, private_key_pem)
        response = self.client.request(
            f"/api/v1/agent-runtime/nodes/{self.client.node_id}/certificate/activate/",
            {},
            tls_context=context,
        )
        if response.get("activated") is not True:
            raise CertificateRotationError("AGENT_TLS_ROTATION_ACTIVATION_REJECTED")
        self._patch_secret({
            "tls.crt": self._encode_secret_value(certificate_pem),
            "tls.key": self._encode_secret_value(private_key_pem),
            "pending.tls.crt": None,
            "pending.tls.key": None,
            "pending.created_at": None,
        })
        self.client.tls_context = context
        return True

    def maintain(self):
        if not self.enabled:
            return False
        if not self.cert_file or not self.key_file or not self.ca_file:
            raise CertificateRotationError("AGENT_TLS_ROTATION_TLS_FILES_REQUIRED")
        now = _utc_now()
        if self.last_check and now - self.last_check < self.check_interval:
            return False
        self.last_check = now
        secret = self._get_secret()
        data = secret.get("data")
        if not isinstance(data, dict):
            raise CertificateRotationError("AGENT_TLS_ROTATION_SECRET_DATA_INVALID")
        if self._activate_pending(data):
            return True
        current_certificate_pem = self._decode_secret_value(data, "tls.crt")
        current_key_pem = self._decode_secret_value(data, "tls.key")
        if not current_certificate_pem or not current_key_pem:
            current_certificate_pem = Path(self.cert_file).read_bytes()
            current_key_pem = Path(self.key_file).read_bytes()
        try:
            current_certificate = x509.load_pem_x509_certificate(current_certificate_pem)
        except ValueError as exc:
            raise CertificateRotationError("AGENT_TLS_ROTATION_ACTIVE_CERTIFICATE_INVALID") from exc
        expires_at = _certificate_time(current_certificate.not_valid_after)
        if expires_at - now > self.threshold:
            return False
        private_key, private_key_pem, csr_pem = self._new_key_and_csr()
        response = self.client.request(
            f"/api/v1/agent-runtime/nodes/{self.client.node_id}/certificate/renew/",
            {"csr_pem": csr_pem},
        )
        certificate_pem = response.get("client_certificate")
        ca_chain_pem = response.get("ca_chain")
        fingerprint = response.get("fingerprint_sha256")
        if (
            not isinstance(certificate_pem, str)
            or not isinstance(ca_chain_pem, str)
            or not isinstance(fingerprint, str)
            or response.get("activation_required") is not True
            or response.get("private_key_returned") is not False
        ):
            raise CertificateRotationError("AGENT_TLS_ROTATION_RESPONSE_INVALID")
        calculated = validate_issued_certificate(
            certificate_pem,
            ca_chain_pem,
            private_key,
            self.client.node_id,
            current_certificate_pem,
        )
        if not hmac.compare_digest(calculated, fingerprint.lower()):
            raise CertificateRotationError("AGENT_TLS_ROTATION_FINGERPRINT_MISMATCH")
        certificate_bytes = (
            certificate_pem.encode("ascii") + ca_chain_pem.encode("ascii")
        )
        self._patch_secret({
            "pending.tls.crt": self._encode_secret_value(certificate_bytes),
            "pending.tls.key": self._encode_secret_value(private_key_pem),
            "pending.created_at": self._encode_secret_value(
                _utc_now().isoformat().encode("ascii")
            ),
        })
        updated_secret = self._get_secret()
        updated_data = updated_secret.get("data", {})
        self._activate_pending(updated_data)
        return True
