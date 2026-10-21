"""Password hashing (stdlib scrypt) and JWT signing for M1 local auth.

Hash format is self-describing: ``scrypt$N$r$p$<salt_hex>$<hash_hex>`` so a
future migration to bcrypt only changes this module, not stored data shape
(新算法可共存解析).
"""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from pydantic import BaseModel

from app.config import Settings

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 64
_ALGORITHM = "HS256"

_HASH_NAME = "scrypt"


class TokenClaims(BaseModel):
    sub: str  # user id
    email: str
    exp: datetime


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_KEY_LEN
    )
    return f"{_HASH_NAME}${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        name, n, r, p, salt_hex, hash_hex = stored_hash.split("$")
    except ValueError:
        return False
    if name != _HASH_NAME:
        return False
    try:
        params = (int(n), int(r), int(p))
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=params[0],
        r=params[1],
        p=params[2],
        dklen=len(expected),
    )
    return hmac.compare_digest(derived, expected)


def create_access_token(settings: Settings, user_id: str, email: str) -> tuple[str, int]:
    expires_in = settings.jwt_expire_minutes * 60
    claims = {
        "sub": user_id,
        "email": email,
        "exp": datetime.now(UTC) + timedelta(seconds=expires_in),
    }
    token = jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=_ALGORITHM)
    return token, expires_in


def decode_access_token(settings: Settings, token: str) -> TokenClaims | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret.get_secret_value(), algorithms=[_ALGORITHM])
        return TokenClaims(sub=payload["sub"], email=payload["email"], exp=payload["exp"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None
