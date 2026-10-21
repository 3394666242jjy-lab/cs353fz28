"""Initialize a local user. Run from the repository root with the backend Python."""

import sys
from getpass import getpass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pymongo.errors import PyMongoError  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services.users import seed_local_user  # noqa: E402


def main() -> int:
    settings = get_settings()
    if settings.app_env not in {"local", "test"}:
        print("Seed accounts are only allowed in local or test environments.", file=sys.stderr)
        return 1
    try:
        if settings.seed_password is not None:
            password = settings.seed_password.get_secret_value()
        else:
            password = getpass("Local test password (12+ characters; input is hidden): ")
            if password != getpass("Repeat password: "):
                print("Passwords do not match. No account was created.", file=sys.stderr)
                return 1
        created = seed_local_user(settings, password)
    except ValueError as exc:
        # Validation messages are fixed strings and contain no user input or secrets.
        print(str(exc), file=sys.stderr)
        return 1
    except PyMongoError:
        print("Database initialization failed. Check MongoDB and its indexes.", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Cancelled. No account was created.", file=sys.stderr)
        return 1
    print("Account created." if created else "Account already exists; password unchanged.")
    print(f"Login email: {settings.seed_email.strip().lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
