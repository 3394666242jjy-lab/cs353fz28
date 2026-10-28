"""Local private object storage adapter for uploaded documents (dev/03).

storage_key/extracted_text_key point at files under ``local_storage_dir`` for
M1; the key layout is private to this adapter so it can be swapped for OSS
later without touching the collections. All keys are validated to stay inside
the storage root before any read or write.
"""

from pathlib import Path
from uuid import uuid4

from app.config import Settings

ALLOWED_SUFFIXES = {".pdf", ".txt", ".md"}


class StorageKeyError(ValueError):
    """Raised when a storage key escapes the storage root."""


def _resolve(settings: Settings, storage_key: str) -> Path:
    root = settings.local_storage_dir.resolve()
    path = (root / storage_key).resolve()
    if path != root and root not in path.parents:
        raise StorageKeyError(f"storage key escapes storage root: {storage_key!r}")
    return path


def save_upload(settings: Settings, user_id: str, filename: str, raw: bytes) -> str:
    """Persist raw upload bytes and return the private storage key."""
    suffix = Path(filename).suffix.lower()
    storage_key = f"uploads/{user_id}/{uuid4().hex}{suffix}"
    path = _resolve(settings, storage_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return storage_key


def read_upload(settings: Settings, storage_key: str) -> bytes | None:
    """Return uploaded bytes, or None when the object is missing."""
    path = _resolve(settings, storage_key)
    if not path.is_file():
        return None
    return path.read_bytes()


def remove_upload(settings: Settings, storage_key: str) -> bool:
    """Best-effort delete; prunes empty parents and returns True if an object was removed."""
    path = _resolve(settings, storage_key)
    if not path.is_file():
        return False
    path.unlink()
    root = settings.local_storage_dir.resolve()
    parent = path.parent
    while parent != root and root in parent.parents:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
    return True


def save_extracted_text(
    settings: Settings, user_id: str, document_id: str, version: int, text: str
) -> str:
    """Persist extracted plain text for a document version; returns its key."""
    storage_key = f"extracted/{user_id}/{document_id}/v{version}.txt"
    path = _resolve(settings, storage_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return storage_key
