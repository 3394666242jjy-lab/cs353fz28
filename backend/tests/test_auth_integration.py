"""Opt-in real MongoDB checks using synthetic, isolated users; never use the seed password."""

import os
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.database import database_connection
from app.services.users import seed_local_user


@pytest.mark.skipif(os.environ.get("RUN_MONGO_AUTH_TESTS") != "1", reason="Needs local MongoDB")
def test_real_login_sessions_and_expiry():
    email = f"auth-{uuid4().hex}@example.invalid"
    settings = Settings(
        _env_file=None, app_env="test", mongodb_database="careerpilot_auth_test", seed_email=email
    )
    password = secrets.token_urlsafe(24)
    with database_connection(settings) as database:
        try:
            assert seed_local_user(settings, password)
            assert not seed_local_user(settings, "different-test-password")
            stored = database.users.find_one({"email": email})
            assert "password" not in stored
            assert stored["password_hash"] != password
            client = TestClient(create_app(settings))
            assert (
                client.post(
                    "/api/v1/auth/login", json={"email": email, "password": "incorrect"}
                ).status_code
                == 401
            )
            response = client.post(
                "/api/v1/auth/login", json={"email": email.upper(), "password": password}
            )
            assert response.status_code == 200
            token = response.json()["data"]["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            me = client.get("/api/v1/me", headers=headers)
            assert me.status_code == 200
            assert me.json()["data"]["id"] == stored["id"]
            assert "password" not in me.text
            assert client.get("/api/v1/me").status_code == 401
            database.auth_sessions.update_many(
                {"user_id": stored["id"]},
                {"$set": {"expires_at": datetime.now(UTC) - timedelta(seconds=1)}},
            )
            assert client.get("/api/v1/me", headers=headers).status_code == 401
        finally:
            # Remove only this run's generated records in the dedicated test database.
            stored = database.users.find_one({"email": email})
            if stored:
                database.auth_sessions.delete_many({"user_id": stored["id"]})
                database.users.delete_one({"id": stored["id"]})
