"""Versioned password hashes; never persist or log plaintext passwords."""

import hashlib
import hmac
import secrets

_FORMAT = "scrypt-v1"
_SALT_BYTES = 16
_HASH_BYTES = 32
_MAX_PASSWORD_BYTES = 4096


def _password_bytes(password: str) -> bytes:
    encoded = password.encode("utf-8")
    if not encoded or len(encoded) > _MAX_PASSWORD_BYTES:
        raise ValueError("Password must contain between 1 and 4096 UTF-8 bytes")
    return encoded


def _derive(password: bytes, salt: bytes) -> bytes:
    # Parameters belong to the format version, not to untrusted stored input.
    return hashlib.scrypt(
        password, salt=salt, n=2**17, r=8, p=1, dklen=_HASH_BYTES, maxmem=256 * 1024 * 1024
    )


def hash_password(password: str) -> str:
    """Generate a random salt and a scrypt hash suitable for storing in MongoDB."""
    encoded = _password_bytes(password)
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _derive(encoded, salt)
    return f"{_FORMAT}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Check a password without accepting malformed hashes or changing whitespace."""
    try:
        encoded = _password_bytes(password)
        if len(stored_hash) != len(_FORMAT) + 2 + 2 * (_SALT_BYTES + _HASH_BYTES):
            return False
        version, salt_hex, digest_hex = stored_hash.split("$")
        if version != _FORMAT:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        if len(salt) != _SALT_BYTES or len(expected) != _HASH_BYTES:
            return False
    except (ValueError, UnicodeError):
        return False
    return hmac.compare_digest(_derive(encoded, salt), expected)
