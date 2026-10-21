import hashlib
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import ConnectionFailure

from app.config import Settings
from app.main import create_app
from app.schemas.resources import TokenResult
from app.services.auth import login_user, resolve_user
from app.services.passwords import hash_password


@pytest.fixture
def settings():
    return Settings(_env_file=None, app_env="test", access_token_ttl_seconds=600)


@pytest.fixture
def database(monkeypatch):
    connection = MagicMock()
    monkeypatch.setattr("app.services.auth.database_connection", connection)
    return connection.return_value.__enter__.return_value


@pytest.fixture(scope="module")
def password_hash():
    return hash_password("synthetic-auth-password")


def test_login_stores_only_token_digest_and_expiration(settings, database, password_hash):
    database.users.find_one.return_value = {"id": "user_one", "password_hash": password_hash}
    result = login_user(settings, " DEMO@EXAMPLE.INVALID ", "synthetic-auth-password")
    assert result is not None
    assert result.expires_in == 600
    database.users.find_one.assert_called_once_with({"email": "demo@example.invalid"})
    record = database.auth_sessions.insert_one.call_args.args[0]
    assert record["_id"] == hashlib.sha256(result.access_token.encode()).hexdigest()
    assert result.access_token not in str(record)
    assert record["user_id"] == "user_one"
    assert (record["expires_at"] - record["created_at"]).total_seconds() == 600
    database.auth_sessions.create_index.assert_any_call("expires_at", expireAfterSeconds=0)


@pytest.mark.parametrize("kind", ["missing", "wrong_password", "corrupt_hash"])
def test_login_failure_never_creates_a_session(settings, database, password_hash, kind):
    database.users.find_one.return_value = {
        "missing": None,
        "wrong_password": {"id": "user_one", "password_hash": password_hash},
        "corrupt_hash": {"id": "user_one", "password_hash": 123},
    }[kind]
    password = "dummy-auth-comparison" if kind == "corrupt_hash" else "wrong-password"
    assert login_user(settings, "demo@example.invalid", password) is None
    database.auth_sessions.insert_one.assert_not_called()


def test_resolve_checks_expiration_and_maps_session_to_user(settings, database):
    database.auth_sessions.find_one.return_value = {"user_id": "user_one"}
    database.users.find_one.return_value = {
        "id": "user_one",
        "email": "demo@example.invalid",
        "display_name": "Demo",
        "password_hash": "must-not-leak",
    }
    user = resolve_user(settings, "a" * 43)
    assert user.id == "user_one"
    assert "password_hash" not in user.model_dump()
    query = database.auth_sessions.find_one.call_args.args[0]
    assert query["expires_at"]["$gt"] <= datetime.now(UTC)
    database.users.find_one.assert_called_once_with({"id": "user_one"})


def test_missing_or_expired_session_is_rejected(settings, database):
    database.auth_sessions.find_one.return_value = None
    assert resolve_user(settings, "a" * 43) is None
    database.users.find_one.assert_not_called()


@pytest.mark.parametrize("token", ["", "a" * 44, "a" * 42, "!" * 43, "密" * 43])
def test_invalid_token_rejected_before_database_lookup(settings, database, token):
    assert resolve_user(settings, token) is None
    database.auth_sessions.find_one.assert_not_called()


def test_login_and_me_error_envelopes_and_documented_security(settings, monkeypatch):
    client = TestClient(create_app(settings))
    for headers in [{}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer invalid"}]:
        response = client.get("/api/v1/me", headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json()["error"]["code"] == "UNAUTHORIZED"
    monkeypatch.setattr("app.api.routes.login_user", lambda *args: None)
    response = client.post("/api/v1/auth/login", json={"email": "unknown", "password": "bad"})
    assert response.status_code == 401
    schema = client.get("/openapi.json").json()
    assert schema["paths"]["/api/v1/me"]["get"]["security"] == [{"HTTPBearer": []}]


def test_login_uses_app_settings_and_returns_existing_envelope(settings, monkeypatch):
    def login(config, email, password):
        assert config is settings
        return TokenResult(access_token="synthetic-token", expires_in=600)

    monkeypatch.setattr("app.api.routes.login_user", login)
    response = TestClient(create_app(settings)).post(
        "/api/v1/auth/login", json={"email": "demo@example.invalid", "password": "synthetic"}
    )
    assert response.status_code == 200
    assert response.json()["data"]["expires_in"] == 600
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_database_failure_is_sanitized(settings, monkeypatch):
    def fail(*args):
        raise ConnectionFailure("DO-NOT-ECHO-CREDENTIALS")

    monkeypatch.setattr("app.api.routes.login_user", fail)
    monkeypatch.setattr("app.api.dependencies.resolve_user", fail)
    client = TestClient(create_app(settings))
    responses = [
        client.post("/api/v1/auth/login", json={"email": "demo", "password": "synthetic"}),
        client.get("/api/v1/me", headers={"Authorization": "Bearer " + "a" * 43}),
    ]
    for response in responses:
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
        assert "DO-NOT-ECHO" not in response.text
