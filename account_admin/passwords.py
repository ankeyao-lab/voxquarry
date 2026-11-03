from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass


ALGORITHM = "pbkdf2-sha256-v1"
ITERATIONS = 310_000
MAX_PASSWORD_CHARACTERS = 1024


class PasswordError(ValueError):
    pass


@dataclass(frozen=True)
class PasswordCredential:
    algorithm: str
    salt_b64: str
    password_hash_b64: str


def validate_password(password: object) -> str:
    if not isinstance(password, str) or not password.strip():
        raise PasswordError("password must not be empty or whitespace")
    if len(password) > MAX_PASSWORD_CHARACTERS:
        raise PasswordError("password is too long")
    return password


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        ITERATIONS,
        dklen=32,
    )


def hash_password(password: object) -> PasswordCredential:
    value = validate_password(password)
    salt = secrets.token_bytes(16)
    derived = _derive(value, salt)
    return PasswordCredential(
        algorithm=ALGORITHM,
        salt_b64=base64.b64encode(salt).decode("ascii"),
        password_hash_b64=base64.b64encode(derived).decode("ascii"),
    )


def verify_password(password: object, credential: PasswordCredential) -> bool:
    try:
        value = validate_password(password)
        salt = base64.b64decode(credential.salt_b64, validate=True)
        expected = base64.b64decode(credential.password_hash_b64, validate=True)
    except (PasswordError, ValueError):
        return False
    if credential.algorithm != ALGORITHM:
        return False
    return hmac.compare_digest(_derive(value, salt), expected)
