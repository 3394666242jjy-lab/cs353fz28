"""Tests for the local private object storage adapter (issue #7)."""

import pytest

from app.config import Settings
from app.services.storage import (
    StorageKeyError,
    read_upload,
    remove_upload,
    save_extracted_text,
    save_upload,
)


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, app_env="test", local_storage_dir=tmp_path / "storage")


def test_upload_roundtrip(settings):
    key = save_upload(settings, "user_one", "resume.md", b"# Demo")
    assert key.startswith("uploads/user_one/")
    assert key.endswith(".md")
    assert read_upload(settings, key) == b"# Demo"
    assert remove_upload(settings, key) is True
    assert read_upload(settings, key) is None
    assert remove_upload(settings, key) is False


def test_read_missing_key_returns_none(settings):
    assert read_upload(settings, "uploads/user_one/nope.md") is None


@pytest.mark.parametrize("bad_key", ["../escape.txt", "uploads/../../x.md"])
def test_keys_escaping_the_root_are_rejected(settings, bad_key):
    with pytest.raises(StorageKeyError):
        read_upload(settings, bad_key)


def test_extracted_text_is_stored_under_versioned_key(settings):
    key = save_extracted_text(settings, "user_one", "doc_x", 2, "hello\nworld")
    assert key == "extracted/user_one/doc_x/v2.txt"
    path = settings.local_storage_dir / key
    assert path.read_text(encoding="utf-8") == "hello\nworld"
