"""Auth smoke tests: seed-account login and token-guarded /me (B #7).

User lookups are monkeypatched so no real MongoDB is required.
"""

import pytest
from fastapi.testclient import TestClient

import app.api.deps as deps
from app.config import Settings
from app.main import create_app
from app.security import create_access_token, hash_password, verify_password


@pytest.fixture
def client():
    return TestClient(create_app(Settings(_env_file=None, app_env="test")))


@pytest.fixture
def seed_user():
    return {
        "id": "usr_test_1",
        "email": "dev@careerpilot.local",
        "password_hash": hash_password("correct horse battery staple"),
        "display_name": "Dev User",
        "timezone": "Asia/Shanghai",
    }


def _login(client, email, password):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def test_password_hash_roundtrip():
    stored = hash_password("s3cret!")
    assert verify_password("s3cret!", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("s3cret!", "garbage")


def test_login_rejects_wrong_credentials(client, monkeypatch, seed_user):
    monkeypatch.setattr(
        "app.api.routes.authenticate_user",
        lambda email, password: seed_user if password == "correct horse battery staple" else None,
    )
    response = _login(client, seed_user["email"], "wrong password")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert "password_hash" not in response.text


def test_login_returns_bearer_token(client, monkeypatch, seed_user):
    monkeypatch.setattr("app.api.routes.authenticate_user", lambda email, password: seed_user)
    response = _login(client, seed_user["email"], "any")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["token_type"] == "bearer"
    assert body["data"]["access_token"]
    assert body["data"]["expires_in"] > 0
    assert body["request_id"] == response.headers["x-request-id"]


def test_me_requires_bearer_token(client):
    response = client.get("/api/v1/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_me_rejects_invalid_token(client):
    response = client.get("/api/v1/me", headers={"Authorization": "Bearer not-a-token"})
    assert response.status_code == 401


def test_me_returns_current_user_without_hash(client, monkeypatch, seed_user):
    monkeypatch.setattr(
        deps, "find_user_by_id", lambda user_id: seed_user if user_id == "usr_test_1" else None
    )
    token, _ = create_access_token(Settings(_env_file=None), seed_user["id"], seed_user["email"])
    response = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data == {
        "id": "usr_test_1",
        "email": "dev@careerpilot.local",
        "display_name": "Dev User",
    }


def test_me_rejects_token_for_deleted_user(client, monkeypatch):
    monkeypatch.setattr(deps, "find_user_by_id", lambda user_id: None)
    token, _ = create_access_token(Settings(_env_file=None), "usr_gone", "gone@example.invalid")
    response = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
