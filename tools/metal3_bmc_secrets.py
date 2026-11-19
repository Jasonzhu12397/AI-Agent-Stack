"""Render and install pre-provisioned Metal3 BMC credential Secrets.

Pre-provision mode keeps BMC credentials out of Cloud Control. The operator
resolves them from the platform credential store, writes an owner-only
manifest file, and applies it to the EPH bootstrap cluster before any
BareMetalHost is created.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any


class BmcSecretError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
_NAMESPACE = re.compile(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?")


def _required_string(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise BmcSecretError(code)
    return value


def render_bmc_secret(machine: Any, *, namespace: str = "metal3") -> dict[str, Any]:
    """Return one Kubernetes Secret for a resolved platform machine."""

    if not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace) or len(namespace) > 63:
        raise BmcSecretError("BMC_SECRET_NAMESPACE_INVALID")
    secret_ref = _required_string(machine.spec.secret_ref, "BMC_SECRET_REFERENCE_INVALID")
    if not _NAME.fullmatch(secret_ref) or len(secret_ref) > 253:
        raise BmcSecretError("BMC_SECRET_REFERENCE_INVALID")
    username = _required_string(machine.username, "BMC_SECRET_USERNAME_MISSING")
    password = _required_string(machine.password, "BMC_SECRET_PASSWORD_MISSING")
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": secret_ref, "namespace": namespace},
        "type": "Opaque",
        "stringData": {"username": username, "password": password},
    }


def render_bmc_secrets(
    machines: Iterable[Any],
    *,
    namespace: str = "metal3",
) -> list[dict[str, Any]]:
    documents = [render_bmc_secret(machine, namespace=namespace) for machine in machines]
    names = [document["metadata"]["name"] for document in documents]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise BmcSecretError("BMC_SECRET_REFERENCE_DUPLICATE")
    return documents


def write_private_manifest(path: Path, documents: list[dict[str, Any]]) -> None:
    """Write a credential manifest readable only by the current user."""

    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump_all(documents, sort_keys=False)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    if path.stat().st_mode & 0o077:
        raise BmcSecretError("BMC_SECRET_MANIFEST_PERMISSIONS_UNSAFE")
