"""Opt-in real JD persistence and cross-user authorization checks."""

import os
import secrets
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.database import database_connection
from app.services.users import seed_local_user


@pytest.mark.skipif(os.environ.get("RUN_MONGO_JOB_TESTS") != "1", reason="Needs local MongoDB")
def test_real_jd_round_trip_and_user_isolation():
    settings = Settings(_env_file=None, app_env="test", mongodb_database="careerpilot_jobs_test")
    emails = [f"jobs-{uuid4().hex}@example.invalid" for _ in range(2)]
    user_ids = []
    password = secrets.token_urlsafe(24)
    with database_connection(settings) as database:
        try:
            headers = []
            client = TestClient(create_app(settings))
            for email in emails:
                account_settings = settings.model_copy(update={"seed_email": email})
                seed_local_user(account_settings, password)
                user_ids.append(database.users.find_one({"email": email})["id"])
                login = client.post(
                    "/api/v1/auth/login", json={"email": email, "password": password}
                )
                assert login.status_code == 200
                headers.append({"Authorization": f"Bearer {login.json()['data']['access_token']}"})

            original_jd = "  合成岗位：后端实习生\n要求：Python、SQL。\n\n"
            payload = {"title": "合成后端岗位", "company": "合成公司", "raw_jd": original_jd}
            assert client.post("/api/v1/jobs", json=payload).status_code == 401
            created = client.post("/api/v1/jobs", json=payload, headers=headers[0])
            assert created.status_code == 201
            job_id = created.json()["data"]["job_id"]
            assert created.json()["data"]["version"] == 1
            owner_result = client.get(f"/api/v1/jobs/{job_id}", headers=headers[0])
            assert owner_result.status_code == 200
            assert owner_result.json()["data"] == {"id": job_id, "version": 1, **payload}
            stored = database.jobs.find_one({"id": job_id})
            assert stored["user_id"] == user_ids[0]
            assert stored["raw_jd"] == original_jd
            assert stored["requirements"] == []
            assert client.get(f"/api/v1/jobs/{job_id}").status_code == 401
            other_user = client.get(f"/api/v1/jobs/{job_id}", headers=headers[1])
            missing = client.get("/api/v1/jobs/missing", headers=headers[0])
            assert other_user.status_code == missing.status_code == 404
            assert other_user.json()["error"] == missing.json()["error"]
            assert (
                client.post(
                    "/api/v1/jobs", json={**payload, "user_id": user_ids[1]}, headers=headers[0]
                ).status_code
                == 422
            )
            assert (
                client.post(
                    "/api/v1/jobs", json={**payload, "raw_jd": " \n"}, headers=headers[0]
                ).status_code
                == 422
            )
        finally:
            # Clean only this run's synthetic users and resources in the dedicated test database.
            for user_id in user_ids:
                database.jobs.delete_many({"user_id": user_id})
                database.auth_sessions.delete_many({"user_id": user_id})
                database.users.delete_one({"id": user_id})
