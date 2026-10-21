"""User lookups for authentication (M1 local seed account)."""

from app.db import get_database
from app.security import verify_password

USERS_COLLECTION = "users"


def find_user_by_email(email: str) -> dict | None:
    normalized = email.strip().lower()
    return get_database()[USERS_COLLECTION].find_one({"email": normalized})


def find_user_by_id(user_id: str) -> dict | None:
    return get_database()[USERS_COLLECTION].find_one({"id": user_id})


def authenticate_user(email: str, password: str) -> dict | None:
    user = find_user_by_email(email)
    if user is None:
        return None
    stored_hash = user.get("password_hash")
    if not isinstance(stored_hash, str):
        return None
    if not verify_password(password, stored_hash):
        return None
    return user


def public_user(user: dict) -> dict:
    """Strip server-only fields before returning a user document."""
    return {
        "id": user["id"],
        "email": user["email"],
        "display_name": user.get("display_name", ""),
    }
