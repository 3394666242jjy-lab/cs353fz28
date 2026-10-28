"""Local account initialization; public registration is outside M1."""

from datetime import UTC, datetime
from uuid import uuid4

from app.config import Settings
from app.services.database import database_connection
from app.services.passwords import hash_password


def seed_local_user(settings: Settings, password: str) -> bool:
    """Create the configured account once; return False without resetting an existing account."""
    if settings.app_env not in {"local", "test"}:
        raise ValueError("Seed accounts are only allowed in local or test environments")
    email = settings.seed_email.strip().lower()
    if not 3 <= len(email) <= 320 or email.count("@") != 1 or any(c.isspace() for c in email):
        raise ValueError("Set SEED_EMAIL to a valid local account email")
    if not settings.seed_display_name.strip():
        raise ValueError("SEED_DISPLAY_NAME cannot be blank")
    if not 12 <= len(password) <= 1024:
        raise ValueError("Use a local test password of 12 to 1024 characters")
    password_hash = hash_password(password)
    now = datetime.now(UTC)
    with database_connection(settings) as database:
        database.users.create_index("email", unique=True)
        database.users.create_index("id", unique=True)
        result = database.users.update_one(
            {"email": email},
            {
                "$setOnInsert": {
                    "id": f"user_{uuid4().hex}",
                    "email": email,
                    "display_name": settings.seed_display_name.strip(),
                    "password_hash": password_hash,
                    "timezone": "Asia/Shanghai",
                    "created_at": now,
                    "updated_at": now,
                }
            },
            upsert=True,
        )
    return result.upserted_id is not None
