# ruff: noqa: I001
import base64
import hashlib
import json
import re

import httpx
from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from django.conf import settings

from core.bmc_credentials import _vault_url, resolve_vault_jwt_token


PEM_PATTERN = re.compile(
    r"-----BEGIN ([A-Z0-9 ]+)-----\s*([A-Za-z0-9+/=\r\n]+?)\s*-----END \1-----",
    re.DOTALL,
)
MOUNT_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
ROLE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class EPHCertificateError(ValueError):
    pass


class EPHCertificateSignerUnavailable(RuntimeError):
    pass


def _decode_pem(value, expected_label, field, maximum):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise EPHCertificateError("EPH_CERTIFICATE_INVALID:" + field)
    match = PEM_PATTERN.fullmatch(value.strip())
    if match is None or match.group(1) != expected_label:
        raise EPHCertificateError("EPH_CERTIFICATE_INVALID:" + field)
    try:
        decoded = base64.b64decode(
            re.sub(r"\s+", "", match.group(2)),
            validate=True,
        )
    except (ValueError, TypeError):
        raise EPHCertificateError("EPH_CERTIFICATE_INVALID:" + field) from None
    if not decoded:
        raise EPHCertificateError("EPH_CERTIFICATE_INVALID:" + field)
    return decoded


def validate_eph_csr(csr_pem, expected_common_name=None):
    _decode_pem(csr_pem, "CERTIFICATE REQUEST", "csr", 16384)
    try:
        csr = x509.load_pem_x509_csr(csr_pem.encode("ascii"))
        common_names = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if not csr.is_signature_valid:
            raise EPHCertificateError("EPH_CERTIFICATE_CSR_SIGNATURE_INVALID")
        if (
            expected_common_name is not None
            and (
                len(common_names) != 1
                or common_names[0].value != expected_common_name
            )
        ):
            raise EPHCertificateError("EPH_CERTIFICATE_CSR_SUBJECT_INVALID")
    except EPHCertificateError:
        raise
    except (ValueError, UnicodeEncodeError, UnsupportedAlgorithm):
        raise EPHCertificateError("EPH_CERTIFICATE_INVALID:csr") from None
    return csr_pem.strip() + "\n"


def _vault_headers(token):
    headers = {
        "X-Vault-Token": token,
        "Content-Type": "application/json",
    }
    namespace = getattr(settings, "BMC_VAULT_NAMESPACE", "")
    if namespace:
        headers["X-Vault-Namespace"] = namespace
    return headers


def _validate_ca_chain(ca_chain, issuing_ca):
    if not isinstance(ca_chain, list) and ca_chain not in (None, ""):
        raise EPHCertificateError("EPH_CERTIFICATE_CA_CHAIN_INVALID")
    values = []
    if issuing_ca:
        values.append(issuing_ca)
    for certificate in ca_chain or []:
        if certificate not in values:
            values.append(certificate)
    if not values or len(values) > 8:
        raise EPHCertificateError("EPH_CERTIFICATE_CA_CHAIN_INVALID")
    normalized = []
    for certificate in values:
        _decode_pem(certificate, "CERTIFICATE", "ca_chain", 131072)
        normalized.append(certificate.strip() + "\n")
    return "".join(normalized)



def _validate_issued_client_certificate(certificate, expected_common_name):
    from datetime import UTC, datetime

    try:
        common_names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        basic_constraints = certificate.extensions.get_extension_for_class(
            x509.BasicConstraints
        ).value
        key_usage = certificate.extensions.get_extension_for_class(
            x509.KeyUsage
        ).value
        extended_key_usage = certificate.extensions.get_extension_for_class(
            x509.ExtendedKeyUsage
        ).value
    except x509.ExtensionNotFound:
        raise EPHCertificateError(
            "EPH_CERTIFICATE_SIGNER_CERTIFICATE_PROFILE_INVALID"
        ) from None

    now = datetime.now(UTC).replace(tzinfo=None)
    if (
        len(common_names) != 1
        or common_names[0].value != expected_common_name
        or basic_constraints.ca
        or not key_usage.digital_signature
        or ExtendedKeyUsageOID.CLIENT_AUTH not in extended_key_usage
    ):
        raise EPHCertificateError(
            "EPH_CERTIFICATE_SIGNER_CERTIFICATE_PROFILE_INVALID"
        )
    if certificate.not_valid_before > now or certificate.not_valid_after <= now:
        raise EPHCertificateError(
            "EPH_CERTIFICATE_SIGNER_CERTIFICATE_VALIDITY_INVALID"
        )


def sign_eph_csr(csr_pem, node_id, tenant_id, project_id):
    common_name = f"agent-node-{node_id}.agents.internal"
    csr_pem = validate_eph_csr(csr_pem, common_name)
    mount = getattr(settings, "EPH_PKI_MOUNT", "pki").strip("/")
    role = getattr(settings, "EPH_PKI_ROLE", "agent-nodes")
    ttl = getattr(settings, "EPH_PKI_TTL", "24h")
    if not MOUNT_PATTERN.fullmatch(mount) or not ROLE_PATTERN.fullmatch(role):
        raise EPHCertificateSignerUnavailable("EPH PKI mount or role is invalid.")
    if (
        not isinstance(ttl, str)
        or not re.fullmatch(r"[1-9][0-9]{0,3}(?:h|m|s)", ttl)
    ):
        raise EPHCertificateSignerUnavailable("EPH PKI certificate TTL is invalid.")

    try:
        address = _vault_url()
        token = resolve_vault_jwt_token(tenant_id, project_id)
    except Exception as exc:
        raise EPHCertificateSignerUnavailable(
            "EPH certificate signer workload identity is unavailable."
        ) from exc

    ca_bundle = getattr(settings, "BMC_VAULT_CA_BUNDLE", "")
    verify = ca_bundle if ca_bundle else True
    timeout_value = float(getattr(settings, "BMC_VAULT_TIMEOUT_SECONDS", 5))
    timeout = httpx.Timeout(timeout_value, connect=min(3.0, timeout_value))
    payload = {
        "csr": csr_pem,
        "common_name": common_name,
        "ttl": ttl,
        "format": "pem",
    }
    try:
        with httpx.Client(
            verify=verify,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        ) as client:
            with client.stream(
                "POST",
                f"{address}/v1/{mount}/sign/{role}",
                headers=_vault_headers(token),
                json=payload,
            ) as response:
                if response.status_code != 200:
                    raise EPHCertificateSignerUnavailable(
                        "EPH certificate signing request was rejected."
                    )
                content_type = (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if content_type != "application/json":
                    raise EPHCertificateError("EPH_CERTIFICATE_SIGNER_RESPONSE_INVALID")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 262144:
                        raise EPHCertificateError("EPH_CERTIFICATE_SIGNER_RESPONSE_TOO_LARGE")
    except (httpx.HTTPError, OSError) as exc:
        raise EPHCertificateSignerUnavailable(
            "EPH certificate signer request failed."
        ) from exc

    try:
        result = json.loads(body)
        data = result.get("data")
        certificate = data.get("certificate")
        issuing_ca = data.get("issuing_ca")
        ca_chain = data.get("ca_chain", [])
    except (TypeError, ValueError, AttributeError):
        raise EPHCertificateError("EPH_CERTIFICATE_SIGNER_RESPONSE_INVALID") from None

    certificate_der = _decode_pem(
        certificate,
        "CERTIFICATE",
        "client_certificate",
        131072,
    )
    try:
        csr = x509.load_pem_x509_csr(csr_pem.encode("ascii"))
        issued_certificate = x509.load_der_x509_certificate(certificate_der)
        csr_public_key = csr.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        issued_public_key = issued_certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if csr_public_key != issued_public_key:
            raise EPHCertificateError("EPH_CERTIFICATE_SIGNER_CERTIFICATE_MISMATCH")
        _validate_issued_client_certificate(issued_certificate, common_name)
    except (ValueError, UnsupportedAlgorithm):
        raise EPHCertificateError("EPH_CERTIFICATE_SIGNER_CERTIFICATE_INVALID") from None
    chain_pem = _validate_ca_chain(ca_chain, issuing_ca)
    return {
        "client_certificate": certificate.strip() + "\n",
        "ca_chain": chain_pem,
        "fingerprint_sha256": hashlib.sha256(certificate_der).hexdigest(),
        "common_name": common_name,
    }
