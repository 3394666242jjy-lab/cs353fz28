"""Create the local M1 seed account. Idempotent: existing email is skipped.

Run from the repository root after ``docker compose up -d``:

    uv run --project backend python scripts/seed_dev_user.py

Credentials come only from the local .env (DEV_SEED_EMAIL / DEV_SEED_PASSWORD);
never commit real credentials to the repository.
"""

import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pymongo import ASCENDING, MongoClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.security import hash_password  # noqa: E402

USERS_COLLECTION = "users"


def main() -> int:
    settings = get_settings()
    if not settings.dev_seed_password:
        print("DEV_SEED_PASSWORD is not set. Add it to your local .env first.", file=sys.stderr)
        return 1

    email = settings.dev_seed_email.strip().lower()
    with MongoClient(settings.mongodb_uri.get_secret_value()) as client:
        database = client[settings.mongodb_database]
        users = database[USERS_COLLECTION]
        users.create_index([(key, ASCENDING) for key in ("email",)], unique=True)

        if users.find_one({"email": email}):
            print(f"Seed account already exists: {email} (skipped)")
            return 0

        now = datetime.now(UTC)
        users.insert_one(
            {
                "id": f"usr_{secrets.token_urlsafe(12)}",
                "email": email,
                "password_hash": hash_password(settings.dev_seed_password.get_secret_value()),
                "display_name": "Dev User",
                "timezone": "Asia/Shanghai",
                "created_at": now,
                "updated_at": now,
            }
        )
    print(f"Seed account created: {email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
