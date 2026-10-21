from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.services.passwords import verify_password
from app.services.users import seed_local_user


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_seed_rejects_nonlocal_environments_before_connecting(environment, monkeypatch):
    connection = MagicMock()
    monkeypatch.setattr("app.services.users.database_connection", connection)
    with pytest.raises(ValueError, match="only allowed"):
        seed_local_user(Settings(_env_file=None, app_env=environment), "synthetic-test-password")
    connection.assert_not_called()


@pytest.mark.parametrize("password", ["", "short", "x" * 1025])
def test_seed_rejects_invalid_password_before_connecting(password, monkeypatch):
    connection = MagicMock()
    monkeypatch.setattr("app.services.users.database_connection", connection)
    with pytest.raises(ValueError, match="12 to 1024"):
        seed_local_user(Settings(_env_file=None, app_env="test"), password)
    connection.assert_not_called()


@pytest.mark.parametrize("created", [True, False])
def test_seed_stores_hash_using_insert_only_update(created, monkeypatch):
    connection = MagicMock()
    database = connection.return_value.__enter__.return_value
    database.users.update_one.return_value.upserted_id = "inserted" if created else None
    monkeypatch.setattr("app.services.users.database_connection", connection)
    settings = Settings(_env_file=None, app_env="test", seed_email=" DEMO@EXAMPLE.INVALID ")
    assert seed_local_user(settings, "synthetic-test-password") is created
    args, kwargs = database.users.update_one.call_args
    assert args[0] == {"email": "demo@example.invalid"}
    assert set(args[1]) == {"$setOnInsert"}
    document = args[1]["$setOnInsert"]
    assert "password" not in document
    assert document["password_hash"] != "synthetic-test-password"
    assert verify_password("synthetic-test-password", document["password_hash"])
    assert kwargs == {"upsert": True}
    database.users.create_index.assert_any_call("email", unique=True)
