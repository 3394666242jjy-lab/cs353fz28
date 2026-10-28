from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pymongo.errors import ConnectionFailure

from app.api.dependencies import current_user
from app.config import Settings
from app.main import create_app
from app.schemas.resources import JobCreate, JobCreated, JobResult, UserResult
from app.services.jobs import load_job, save_job


@pytest.fixture
def settings():
    return Settings(_env_file=None, app_env="test")


@pytest.fixture
def database(monkeypatch):
    connection = MagicMock()
    monkeypatch.setattr("app.services.jobs.database_connection", connection)
    return connection.return_value.__enter__.return_value


@pytest.fixture
def client(settings):
    app = create_app(settings)
    app.dependency_overrides[current_user] = lambda: UserResult(
        id="user_one", email="demo@example.invalid", display_name="Demo"
    )
    return TestClient(app)


def test_job_persists_owner_version_and_exact_jd(settings, database):
    text = "  Synthetic JD\nPython\n\n"
    payload = JobCreate(title="Backend intern", raw_jd=text)
    created = save_job(settings, "user_one", payload)
    document = database.jobs.insert_one.call_args.args[0]
    assert document["id"] == created.job_id
    assert document["user_id"] == "user_one"
    assert document["version"] == created.version == 1
    assert document["raw_jd"] == text
    assert document["requirements"] == []
    assert document["created_at"] == document["updated_at"]
    database.jobs.create_index.assert_any_call("id", unique=True)


def test_job_lookup_filters_owner_and_hides_internal_fields(settings, database):
    database.jobs.find_one.return_value = {
        "id": "job_one",
        "user_id": "user_one",
        "title": "Demo",
        "raw_jd": "Synthetic",
        "version": 1,
        "_id": "mongo-internal",
        "requirements": [],
    }
    result = load_job(settings, "user_one", "job_one")
    database.jobs.find_one.assert_called_once_with({"id": "job_one", "user_id": "user_one"})
    assert set(result.model_dump()) == {"id", "title", "company", "raw_jd", "version"}


def test_missing_job_returns_no_result(settings, database):
    database.jobs.find_one.return_value = None
    assert load_job(settings, "user_one", "missing") is None


@pytest.mark.parametrize("text", ["", " \n\t ", "x" * 30001])
def test_invalid_jd_is_rejected(text):
    with pytest.raises(ValidationError):
        JobCreate(title="Demo", raw_jd=text)


def test_routes_require_authentication(settings):
    client = TestClient(create_app(settings))
    assert (
        client.post("/api/v1/jobs", json={"title": "Demo", "raw_jd": "Synthetic"}).status_code
        == 401
    )
    assert client.get("/api/v1/jobs/any").status_code == 401


def test_create_and_query_contracts(client, settings, monkeypatch):
    def save(config, user_id, payload):
        assert config is settings
        assert user_id == "user_one"
        return JobCreated(job_id="job_synthetic", version=1)

    monkeypatch.setattr("app.api.routes.save_job", save)
    response = client.post("/api/v1/jobs", json={"title": "Demo", "raw_jd": "Synthetic"})
    assert response.status_code == 201
    assert response.json()["data"] == {"job_id": "job_synthetic", "version": 1}

    def load(config, user_id, job_id):
        assert user_id == "user_one" and job_id == "job_synthetic"
        return JobResult(id=job_id, title="Demo", raw_jd="Synthetic", version=1)

    monkeypatch.setattr("app.api.routes.load_job", load)
    response = client.get("/api/v1/jobs/job_synthetic")
    assert response.status_code == 200
    assert response.json()["data"]["id"] == "job_synthetic"
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_unowned_and_missing_job_have_same_404(client, monkeypatch):
    monkeypatch.setattr("app.api.routes.load_job", lambda *args: None)
    response = client.get("/api/v1/jobs/someone-elses-job")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_database_error_does_not_expose_jd_or_credentials(client, monkeypatch):
    def fail(*args):
        raise ConnectionFailure("DO-NOT-ECHO")

    monkeypatch.setattr("app.api.routes.save_job", fail)
    monkeypatch.setattr("app.api.routes.load_job", fail)
    responses = [
        client.post("/api/v1/jobs", json={"title": "Demo", "raw_jd": "Synthetic"}),
        client.get("/api/v1/jobs/job_one"),
    ]
    for response in responses:
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
        assert "DO-NOT-ECHO" not in response.text
