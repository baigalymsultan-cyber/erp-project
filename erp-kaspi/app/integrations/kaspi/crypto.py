"""AES-256-GCM envelope encryption for Kaspi tokens (§18, §19, §71).

Envelope layout: key_version(1 byte) || nonce(12 bytes) || ciphertext || tag(16).
The master key never leaves the environment (§10); tokens are never logged
(§71) — only a masked preview is allowed.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_BYTES = 12
_TAG_BYTES = 16
_KEY_VERSION = 1


class MasterKeyMissing(RuntimeError):
    """ENCRYPTION_KEY is absent from the environment (§10)."""


def load_master_key(environ: dict[str, str] | None = None) -> bytes:
    """Load the 32-byte master key from the environment (§10)."""
    import os as _os

    env = environ if environ is not None else _os.environ
    # ENCRYPTION_KEY is the canonical name (.env.example, ADR-009). The legacy
    # KASPI_MASTER_KEY name is still honoured so existing deployments keep
    # working, but the documentation no longer mentions it.
    raw = env.get("ENCRYPTION_KEY") or env.get("KASPI_MASTER_KEY")
    if not raw:
        raise MasterKeyMissing(
            "ENCRYPTION_KEY is not set — add a base64-encoded 32-byte key "
            "to the environment (§10: no defaults for secrets)"
        )
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise ValueError(f"ENCRYPTION_KEY must decode to 32 bytes, got {len(key)}")
    return key


def _as_key(master_key: bytes | str) -> bytes:
    """Accept raw 32 bytes or a base64 string (the env format)."""
    if isinstance(master_key, str):
        return base64.b64decode(master_key)
    return master_key


def encrypt_token(token: str, master_key: bytes | str) -> bytes:
    """Seal a token into the versioned AES-256-GCM envelope."""
    key = _as_key(master_key)
    aes = AESGCM(key)
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = aes.encrypt(nonce, token.encode("utf-8"), None)
    return bytes([_KEY_VERSION]) + nonce + ciphertext


def decrypt_token(envelope: bytes, master_key: bytes | str) -> str:
    """Open the envelope; a wrong key or tampered blob raises ValueError."""
    key = _as_key(master_key)
    if len(envelope) < 1 + _NONCE_BYTES + _TAG_BYTES:
        raise ValueError("kaspi token envelope is truncated")
    version = envelope[0]
    if version != _KEY_VERSION:
        raise ValueError(f"unsupported key version {version}")
    nonce = envelope[1 : 1 + _NONCE_BYTES]
    ciphertext = envelope[1 + _NONCE_BYTES :]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")
    except InvalidTag as exc:
        raise ValueError("kaspi token envelope failed authentication (wrong key?)") from exc


def masked_token_preview(envelope: bytes) -> str:
    """SHA-256 fingerprint prefix — safe for logs (§71)."""
    return "sha256:" + hashlib.sha256(envelope).hexdigest()[:12]
