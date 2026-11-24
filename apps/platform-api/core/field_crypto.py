"""Field-level sealing for values that must be stored but never echoed.

One AES-GCM implementation serves every sealed field: payment merchant keys,
notification webhook URLs (which embed a bot token) and their signing secrets.
The scope string is mixed into the key derivation so a blob sealed for one
purpose cannot be replayed into another.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_BYTES = 12


def _key(scope: str) -> bytes:
    from django.conf import settings

    material = f"{scope}:{settings.SECRET_KEY}".encode()
    return hashlib.sha256(material).digest()


def seal(plaintext: str, scope: str) -> str:
    """Encrypt one value into a storable string."""

    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(_key(scope)).encrypt(nonce, plaintext.encode("utf-8"), scope.encode())
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def open_sealed(blob: str, scope: str) -> str | None:
    """Decrypt a stored value, or ``None`` when it cannot be read."""

    if not blob:
        return None
    try:
        raw = base64.urlsafe_b64decode(blob.encode("ascii"))
        nonce, ciphertext = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        return AESGCM(_key(scope)).decrypt(nonce, ciphertext, scope.encode()).decode("utf-8")
    except Exception:
        # A rotated SECRET_KEY makes old values unreadable; callers then report
        # the field as unset instead of failing a request.
        return None
