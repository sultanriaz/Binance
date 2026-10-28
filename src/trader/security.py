"""Minimal operator authentication: scrypt credentials, TOTP and HMAC JWT.

No trading permissions exist. Never expose TOTP secrets through HTTP.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from typing import Any


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(encoded: str) -> bytes:
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))


def password_hash(password: str, salt: bytes | None = None) -> str:
    if len(password) < 14:
        raise ValueError("Password must have at least 14 characters")
    salt = salt or os.urandom(16)
    value = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$16384$" + b64(salt) + "$" + b64(value)


def password_verify(password: str, encoded: str) -> bool:
    try:
        method, cost, salt, want = encoded.split("$")
        if method != "scrypt" or cost != "16384":
            return False
        actual = hashlib.scrypt(password.encode(), salt=unb64(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, unb64(want))
    except (ValueError, TypeError):
        return False


def totp(secret: str, tick: int | None = None) -> str:
    try:
        key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8), casefold=True)
    except Exception as exc:
        raise ValueError("invalid TOTP secret") from exc
    counter = int((tick if tick is not None else time.time()) // 30)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    token = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{token:06d}"


def verify_totp(secret: str, code: str, when: int | None = None) -> bool:
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return False
    now = when if when is not None else int(time.time())
    return any(hmac.compare_digest(totp(secret, now + delta), code) for delta in (-30, 0, 30))


def issue_token(user_id: int, role: str, key: str, now: int | None = None) -> str:
    now = now if now is not None else int(time.time())
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = b64(json.dumps({"sub": str(user_id), "role": role, "iat": now, "exp": now + 900}, separators=(",", ":")).encode())
    sig = b64(hmac.new(key.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


def check_token(token: str, key: str, now: int | None = None) -> dict[str, Any]:
    try:
        head, body, signature = token.split(".")
        expected = b64(hmac.new(key.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid signature")
        header = json.loads(unb64(head))
        payload = json.loads(unb64(body))
        current = now if now is not None else int(time.time())
        if (header != {"alg": "HS256", "typ": "JWT"} or
            not isinstance(payload.get("exp"), int) or payload["exp"] <= current or
            not isinstance(payload.get("iat"), int) or payload["iat"] > current + 60 or
            payload.get("role") not in ("ADMIN", "VIEWER") or
            not str(payload.get("sub", "")).isdigit()):
            raise ValueError("invalid token claims")
        return payload
    except (ValueError, json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid/expired token") from exc


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
