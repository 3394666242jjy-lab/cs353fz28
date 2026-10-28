"""MongoDB-backed bearer sessions. Raw access tokens are returned only at login."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from app.config import Settings
from app.schemas.resources import TokenResult, UserResult
from app.services.database import database_connection
from app.services.passwords import hash_password, verify_password


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    # Unknown accounts still perform a password derivation, reducing account enumeration.
    return hash_password("dummy-auth-comparison")


def login_user(settings: Settings, email: str, password: str) -> TokenResult | None:
    with database_connection(settings) as database:
        user = database.users.find_one({"email": email.strip().lower()})
        stored_hash = user.get("password_hash") if user else None
        usable_hash = stored_hash if isinstance(stored_hash, str) else _dummy_hash()
        matches = verify_password(password, usable_hash)
        if user is None or not isinstance(stored_hash, str) or not matches:
            return None
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        database.auth_sessions.create_index("expires_at", expireAfterSeconds=0)
        database.auth_sessions.create_index("user_id")
        database.auth_sessions.insert_one(
            {
                "_id": hashlib.sha256(token.encode("ascii")).hexdigest(),
                "user_id": user["id"],
                "created_at": now,
                "expires_at": now + timedelta(seconds=settings.access_token_ttl_seconds),
            }
        )
    return TokenResult(access_token=token, expires_in=settings.access_token_ttl_seconds)


def resolve_user(settings: Settings, token: str) -> UserResult | None:
    # Only the exact token format emitted by login is accepted; bound lookup input size.
    if len(token) != 43 or any(
        c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in token
    ):
        return None
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    with database_connection(settings) as database:
        session = database.auth_sessions.find_one(
            {"_id": token_hash, "expires_at": {"$gt": datetime.now(UTC)}}
        )
        if session is None:
            return None
        user = database.users.find_one({"id": session["user_id"]})
        if user is None:
            return None
        return UserResult(id=user["id"], email=user["email"], display_name=user["display_name"])
