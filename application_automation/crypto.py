"""Small deterministic cryptographic primitives; callers own key custody."""
from __future__ import annotations

import base64
from decimal import Decimal
import hashlib
import hmac
import json
import math
import os
from dataclasses import dataclass
from typing import Any, Mapping
from cryptography.exceptions import InvalidTag

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CryptoError(ValueError):
    pass


def canonical_json(value: Any) -> bytes:
    """Encode supported JSON values according to RFC 8785's JCS profile."""

    def number(item: int | float) -> str:
        if isinstance(item, bool):
            raise CryptoError("booleans are not JSON numbers")
        if isinstance(item, int):
            if abs(item) > 9_007_199_254_740_991:
                raise CryptoError("canonical JSON integers must be IEEE-754 safe")
            return str(item)
        if not math.isfinite(item):
            raise CryptoError("canonical JSON does not support non-finite numbers")
        if item == 0:
            return "0"
        rendered = repr(item)
        mantissa, marker, exponent = rendered.partition("e")
        if not marker:
            return mantissa[:-2] if mantissa.endswith(".0") else mantissa
        exponent_value = int(exponent)
        # ECMAScript uses decimal notation for [1e-6, 1e21).
        absolute = abs(item)
        if 1e-6 <= absolute < 1e21:
            return format(Decimal(rendered), "f")
        mantissa = mantissa[:-2] if mantissa.endswith(".0") else mantissa
        return f"{mantissa}e{'+' if exponent_value >= 0 else ''}{exponent_value}"

    def encode(item: Any) -> str:
        if item is None:
            return "null"
        if item is True:
            return "true"
        if item is False:
            return "false"
        if isinstance(item, str):
            return json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            return number(item)
        if isinstance(item, (list, tuple)):
            return "[" + ",".join(encode(child) for child in item) + "]"
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise CryptoError("canonical JSON object keys must be strings")
            # JCS sorts member names by UTF-16 code units, not Unicode code points.
            keys = sorted(item, key=lambda key: key.encode("utf-16be"))
            return "{" + ",".join(f"{encode(key)}:{encode(item[key])}" for key in keys) + "}"
        raise CryptoError(f"unsupported canonical JSON type: {type(item).__name__}")

    return encode(value).encode("utf-8")


def _frame(domain: str, payload: bytes) -> bytes:
    domain_bytes = domain.encode("utf-8")
    if not domain_bytes:
        raise CryptoError("domain must not be empty")
    return b"application_automation\x00" + len(domain_bytes).to_bytes(4, "big") + domain_bytes + len(payload).to_bytes(8, "big") + payload


def domain_hmac(key: bytes, domain: str, value: Any) -> str:
    if not isinstance(key, bytes) or not key:
        raise CryptoError("HMAC key must be non-empty bytes")
    if not isinstance(domain, str) or not domain:
        raise CryptoError("domain must be a non-empty string")
    payload = canonical_json(value)
    return hmac.new(key, _frame(domain, payload), hashlib.sha256).hexdigest()


def verify_domain_hmac(key: bytes, domain: str, value: Any, signature: str) -> bool:
    if not isinstance(signature, str) or len(signature) != 64:
        return False
    try:
        expected = domain_hmac(key, domain, value)
    except (CryptoError, TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, signature)


def sha256_artifact(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str, *, allow_empty: bool = False) -> bytes:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise CryptoError("base64url field must be a non-empty string")
    if "=" in value:
        raise CryptoError("base64url field must be unpadded")
    try:
        decoded = base64.b64decode(value.encode("ascii") + b"=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise CryptoError("invalid base64url field") from exc
    if _b64url_encode(decoded) != value:
        raise CryptoError("base64url field is not canonically encoded")
    return decoded


@dataclass(frozen=True)
class AesGcmEnvelope:
    version: int
    nonce: str
    ciphertext: str
    tag: str

    def to_dict(self) -> dict[str, object]:
        return {"ciphertext": self.ciphertext, "nonce": self.nonce, "tag": self.tag, "version": self.version}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "AesGcmEnvelope":
        if (
            not isinstance(value, Mapping)
            or set(value) != {"version", "nonce", "ciphertext", "tag"}
            or type(value["version"]) is not int
            or value["version"] != 1
        ):
            raise CryptoError("unsupported envelope")
        if not all(isinstance(value[name], str) for name in ("nonce", "ciphertext", "tag")):
            raise CryptoError("invalid envelope fields")
        return cls(version=1, nonce=value["nonce"], ciphertext=value["ciphertext"], tag=value["tag"])


def _aad(domain: str, aad: Mapping[str, Any] | None) -> bytes:
    return _frame(domain + ".aad", canonical_json({} if aad is None else dict(aad)))


def encrypt_aes_gcm(key: bytes, plaintext: bytes, *, domain: str, aad: Mapping[str, Any] | None = None) -> AesGcmEnvelope:
    """Encrypt bytes; an empty plaintext is represented by an empty ciphertext field."""
    if len(key) != 32:
        raise CryptoError("AES-256-GCM requires a 32-byte key")
    nonce = os.urandom(12)
    encrypted = AESGCM(key).encrypt(nonce, plaintext, _aad(domain, aad))
    return AesGcmEnvelope(1, _b64url_encode(nonce), _b64url_encode(encrypted[:-16]), _b64url_encode(encrypted[-16:]))


def decrypt_aes_gcm(key: bytes, envelope: AesGcmEnvelope | Mapping[str, object], *, domain: str, aad: Mapping[str, Any] | None = None) -> bytes:
    if len(key) != 32:
        raise CryptoError("AES-256-GCM requires a 32-byte key")
    parsed = AesGcmEnvelope.from_dict(envelope) if isinstance(envelope, Mapping) else envelope
    if parsed.version != 1:
        raise CryptoError("unsupported envelope version")
    nonce = _b64url_decode(parsed.nonce)
    ciphertext = _b64url_decode(parsed.ciphertext, allow_empty=True)
    tag = _b64url_decode(parsed.tag)
    if len(nonce) != 12 or len(tag) != 16:
        raise CryptoError("invalid AES-GCM nonce or tag length")
    associated_data = _aad(domain, aad)
    try:
        return AESGCM(key).decrypt(nonce, ciphertext + tag, associated_data)
    except InvalidTag as exc:
        raise CryptoError("AES-GCM authentication failed") from exc
