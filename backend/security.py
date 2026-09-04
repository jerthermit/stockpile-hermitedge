import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

from config import auth_secret, token_ttl_seconds


PASSWORD_ITERATIONS = 310_000


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _hash_secret(value: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        value.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${_b64encode(salt)}${_b64encode(digest)}"


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("Configured passwords must contain at least 10 characters.")
    return _hash_secret(password)


def hash_badge_pin(pin: str) -> str:
    if not (4 <= len(pin) <= 12) or not pin.isdigit():
        raise ValueError("Configured badge PINs must contain 4 to 12 digits.")
    return _hash_secret(pin)


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_value, digest_value = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        expected = _b64decode(digest_value)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            _b64decode(salt_value),
            int(iterations),
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def issue_token(user_id: str) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + token_ttl_seconds(),
        "nonce": secrets.token_urlsafe(12),
    }
    body = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = hmac.new(
        auth_secret().encode("utf-8"),
        body.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{body}.{_b64encode(signature)}"


def verify_token(token: str) -> dict[str, Any] | None:
    try:
        body, signature_value = token.split(".", 1)
        expected = hmac.new(
            auth_secret().encode("utf-8"),
            body.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(expected, _b64decode(signature_value)):
            return None
        payload = json.loads(_b64decode(body))
        if not isinstance(payload.get("sub"), str):
            return None
        if int(payload.get("exp", 0)) <= int(time.time()):
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, binascii.Error):
        return None
