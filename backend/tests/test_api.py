from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import current_user
from app.config import Settings
from app.main import create_app
from app.schemas.resources import DocumentResult, UserResult

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client():
    return TestClient(create_app(Settings(_env_file=None, app_env="test")))


def test_liveness_and_capabilities_do_not_claim_business_is_implemented(client):
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "ok"
    assert response.headers["x-request-id"] == response.json()["request_id"]
    meta = client.get("/api/v1/meta").json()["data"]
    assert meta["mode"] == "scaffold"
    assert meta["features"]["authentication"] is True
    assert meta["features"]["job_import"] is True
    assert all(
        value is False
        for key, value in meta["features"].items()
        if key not in {"authentication", "job_import", "document_upload"}
    )


@pytest.mark.parametrize("all_ready", [True, False])
def test_readiness_uses_dependency_results(monkeypatch, client, all_ready):
    async def probe(settings):
        return {"mongodb": "ok", "redis": "ok" if all_ready else "unavailable", "qdrant": "ok"}

    monkeypatch.setattr("app.main.check_dependencies", probe)
    response = client.get("/health/ready")
    assert response.status_code == (200 if all_ready else 503)
    assert response.json()["data"]["status"] == ("ready" if all_ready else "not_ready")


def test_dependency_errors_never_echo_connection_credentials(monkeypatch, client):
    def fail_mongo(settings):
        raise RuntimeError("mongodb://private-user:DO-NOT-ECHO@private-host")

    async def ok(settings):
        return True

    monkeypatch.setattr("app.services.readiness._ping_mongo", fail_mongo)
    monkeypatch.setattr("app.services.readiness._ping_redis", ok)
    monkeypatch.setattr("app.services.readiness._ping_qdrant", ok)
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["data"]["dependencies"]["mongodb"] == "unavailable"
    assert "DO-NOT-ECHO" not in response.text
    assert "private-host" not in response.text


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/api/v1/analyses", {"job_id": "job_1", "document_ids": ["doc_1"]}),
        ("get", "/api/v1/analyses/analysis_1", None),
    ],
)
def test_unimplemented_business_never_returns_fake_success(client, method, path, payload):
    response = (
        client.request(method, path, json=payload) if payload else client.request(method, path)
    )
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "NOT_IMPLEMENTED"
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert "data" not in response.json()


def _auth_as_user_one(client):
    client.app.dependency_overrides[current_user] = lambda: UserResult(
        id="user_one", email="demo@example.invalid", display_name="Demo"
    )


def _multipart(content: bytes, filename: str = "demo.md"):
    return {"file": (filename, content, "application/octet-stream")}


def test_upload_requires_authentication(client):
    response = client.post(
        "/api/v1/documents", files=_multipart(b"synthetic input"), data={"kind": "resume"}
    )
    assert response.status_code == 401


def test_upload_rejects_empty_file(client):
    _auth_as_user_one(client)
    response = client.post(
        "/api/v1/documents", files=_multipart(b"", "empty.md"), data={"kind": "resume"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"


def test_upload_rejects_oversized_file(client, monkeypatch):
    _auth_as_user_one(client)
    monkeypatch.setattr(
        "app.api.routes.create_document",
        lambda *args, **kwargs: pytest.fail("oversized upload must not persist"),
    )
    response = client.post(
        "/api/v1/documents",
        files=_multipart(b"x" * (10 * 1024 * 1024 + 1), "big.md"),
        data={"kind": "resume"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_upload_rejects_unsupported_file_type(client, monkeypatch):
    _auth_as_user_one(client)
    monkeypatch.setattr(
        "app.api.routes.create_document",
        lambda *args, **kwargs: pytest.fail("unsupported upload must not persist"),
    )
    response = client.post(
        "/api/v1/documents", files=_multipart(b"content", "demo.docx"), data={"kind": "project"}
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"


def test_upload_persists_file_and_dispatches_index_task(client, tmp_path, monkeypatch):
    _auth_as_user_one(client)
    monkeypatch.setattr(client.app.state.settings, "local_storage_dir", tmp_path / "storage")
    monkeypatch.setattr("app.api.routes.create_document", lambda *a, **k: "doc_new")
    monkeypatch.setattr("app.api.routes.create_execution", lambda *a, **k: "exec_new")
    dispatched = {}
    monkeypatch.setattr(
        "app.api.routes.dispatch_index_task",
        lambda settings, **kwargs: dispatched.update(kwargs),
    )
    response = client.post(
        "/api/v1/documents",
        files=_multipart(b"# Demo resume", "resume.md"),
        data={"kind": "resume"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["data"] == {
        "document_id": "doc_new",
        "execution_id": "exec_new",
        "status": "processing",
    }
    assert dispatched["user_id"] == "user_one"
    assert dispatched["document_id"] == "doc_new"
    assert dispatched["version"] == 1
    assert dispatched["execution_id"] == "exec_new"
    stored = (tmp_path / "storage" / dispatched["storage_key"]).read_bytes()
    assert stored == b"# Demo resume"


def test_upload_dispatch_failure_fails_execution_and_document(client, tmp_path, monkeypatch):
    _auth_as_user_one(client)
    monkeypatch.setattr(client.app.state.settings, "local_storage_dir", tmp_path / "storage")
    monkeypatch.setattr("app.api.routes.create_document", lambda *a, **k: "doc_new")
    monkeypatch.setattr("app.api.routes.create_execution", lambda *a, **k: "exec_new")

    def broken_dispatch(settings, **kwargs):
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr("app.api.routes.dispatch_index_task", broken_dispatch)
    transitions = []
    monkeypatch.setattr(
        "app.api.routes.transition_execution",
        lambda settings, user_id, execution_id, **kwargs: (
            transitions.append((execution_id, kwargs)) or True
        ),
    )
    statuses = []
    monkeypatch.setattr(
        "app.api.routes.set_document_status",
        lambda settings, user_id, document_id, **kwargs: statuses.append((document_id, kwargs)),
    )
    response = client.post(
        "/api/v1/documents",
        files=_multipart(b"# Demo resume", "resume.md"),
        data={"kind": "resume"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "TASK_DISPATCH_FAILED"
    assert transitions[0][0] == "exec_new"
    assert transitions[0][1]["status"] == "failed"
    assert transitions[0][1]["error"].code == "TASK_DISPATCH_FAILED"
    assert statuses == [("doc_new", {"status": "failed", "error": "索引任务投递失败，请重试。"})]


def test_upload_database_failure_removes_stored_file(client, tmp_path, monkeypatch):
    from pymongo.errors import PyMongoError

    _auth_as_user_one(client)
    monkeypatch.setattr(client.app.state.settings, "local_storage_dir", tmp_path / "storage")

    def failing_create_document(*args, **kwargs):
        raise PyMongoError("down")

    monkeypatch.setattr("app.api.routes.create_document", failing_create_document)
    response = client.post(
        "/api/v1/documents",
        files=_multipart(b"# Demo resume", "resume.md"),
        data={"kind": "resume"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert list((tmp_path / "storage").rglob("*")) == []


def test_evidence_requires_authentication(client):
    assert client.get("/api/v1/evidence/chunk_1?version=1").status_code == 401


def test_evidence_returns_quote_locator_and_source(client, monkeypatch):
    _auth_as_user_one(client)
    monkeypatch.setattr(
        "app.api.routes.chunks_get",
        lambda settings, user_id, chunk_id, version: {
            "id": chunk_id,
            "document_id": "doc_x",
            "version": version,
            "corpus": "personal",
            "text": "led a five-person team",
            "locator": {"kind": "pdf", "page": 3},
        },
    )
    monkeypatch.setattr(
        "app.api.routes.documents_get",
        lambda settings, user_id, document_id: DocumentResult(
            id=document_id,
            kind="resume",
            filename="resume.pdf",
            current_version=2,
            status="ready",
        ),
    )
    response = client.get("/api/v1/evidence/chunk_1?version=2")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data == {
        "chunk_id": "chunk_1",
        "document_id": "doc_x",
        "version": 2,
        "filename": "resume.pdf",
        "source_type": "personal",
        "locator": {"kind": "pdf", "page": 3},
        "quote": "led a five-person team",
    }


def test_evidence_maps_text_locator_lines(client, monkeypatch):
    _auth_as_user_one(client)
    monkeypatch.setattr(
        "app.api.routes.chunks_get",
        lambda settings, user_id, chunk_id, version: {
            "id": chunk_id,
            "document_id": "doc_x",
            "version": version,
            "corpus": "personal",
            "text": "some lines",
            "locator": {"kind": "text", "line_start": 10, "line_end": 18},
        },
    )
    monkeypatch.setattr(
        "app.api.routes.documents_get",
        lambda settings, user_id, document_id: DocumentResult(
            id=document_id,
            kind="project",
            filename="project.md",
            current_version=1,
            status="ready",
        ),
    )
    response = client.get("/api/v1/evidence/chunk_t?version=1")
    assert response.status_code == 200
    assert response.json()["data"]["locator"] == {
        "kind": "text",
        "line_start": 10,
        "line_end": 18,
    }


@pytest.mark.parametrize(
    ("chunk_row", "document"),
    [
        (
            None,
            DocumentResult(
                id="doc_x", kind="resume", filename="r.pdf", current_version=1, status="ready"
            ),
        ),
        (
            {
                "id": "chunk_1",
                "document_id": "doc_x",
                "version": 1,
                "corpus": "personal",
                "text": "quote",
                "locator": {"kind": "pdf", "page": 1},
            },
            None,
        ),
    ],
)
def test_evidence_not_found_when_chunk_or_document_missing(
    client, monkeypatch, chunk_row, document
):
    _auth_as_user_one(client)
    monkeypatch.setattr(
        "app.api.routes.chunks_get",
        lambda settings, user_id, chunk_id, version: chunk_row,
    )
    monkeypatch.setattr("app.api.routes.documents_get", lambda settings, user_id, doc_id: document)
    response = client.get("/api/v1/evidence/chunk_1?version=1")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_document_queries_require_authentication(client):
    assert client.get("/api/v1/documents").status_code == 401
    assert client.get("/api/v1/documents/doc_1").status_code == 401


def test_document_queries_are_owner_scoped(client, monkeypatch):
    seen = {}
    client.app.dependency_overrides[current_user] = lambda: UserResult(
        id="user_one", email="demo@example.invalid", display_name="Demo"
    )

    def fake_list(settings, user_id, cursor=None, limit=20):
        seen["user_id"] = user_id
        return (
            [
                DocumentResult(
                    id="doc_1",
                    kind="resume",
                    filename="resume.pdf",
                    current_version=1,
                    status="uploaded",
                )
            ],
            None,
        )

    monkeypatch.setattr("app.api.routes.documents_list", fake_list)
    response = client.get("/api/v1/documents")
    assert response.status_code == 200
    assert seen["user_id"] == "user_one"
    assert response.json()["data"]["items"][0]["id"] == "doc_1"


def test_document_detail_returns_not_found_for_missing_document(client, monkeypatch):
    monkeypatch.setattr("app.api.routes.documents_get", lambda settings, user_id, doc_id: None)
    client.app.dependency_overrides[current_user] = lambda: UserResult(
        id="user_one", email="demo@example.invalid", display_name="Demo"
    )
    response = client.get("/api/v1/documents/doc_missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_validation_rejects_user_override_without_echoing_input(client):
    client.app.dependency_overrides[current_user] = lambda: UserResult(
        id="user_one", email="demo@example.invalid", display_name="Demo"
    )
    response = client.post(
        "/api/v1/jobs",
        json={"title": "Demo", "raw_jd": "Synthetic JD", "user_id": "DO-NOT-ECHO-ME"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    assert "DO-NOT-ECHO-ME" not in response.text


def test_invalid_evidence_version_and_unknown_routes_have_envelopes(client):
    _auth_as_user_one(client)
    assert (
        client.get("/api/v1/evidence/chunk_1?version=0").json()["error"]["code"] == "INVALID_INPUT"
    )
    response = client.get("/missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_unexpected_errors_are_sanitized(monkeypatch):
    async def broken(settings):
        raise RuntimeError("DO-NOT-ECHO-INTERNALS")

    monkeypatch.setattr("app.main.check_dependencies", broken)
    client = TestClient(create_app(Settings(_env_file=None)), raise_server_exceptions=False)
    response = client.get("/health/ready")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "DO-NOT-ECHO-INTERNALS" not in response.text


def test_openapi_describes_shared_models_and_stub_responses(client):
    schema = client.get("/openapi.json").json()
    assert "AnalysisResult" in schema["components"]["schemas"]
    assert "501" in schema["paths"]["/api/v1/analyses"]["post"]["responses"]
    validation_response = schema["paths"]["/api/v1/jobs"]["post"]["responses"]["422"]
    assert validation_response["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorEnvelope"
    )
    assert client.get("/docs").status_code == 200
