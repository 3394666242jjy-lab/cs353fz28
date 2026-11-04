"""Route-level tests for the M2 backend surface (issue #24).

Covers document version/delete, SSE replay contract, resume validation and
the plan flow. Services are mocked at the route boundary; user isolation is
proven by overriding the authenticated user and observing owner-scoped calls.
"""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import current_user
from app.config import Settings
from app.main import create_app
from app.schemas.resources import DocumentResult, ExecutionResult, UserResult
from app.services.inputs import InputConflictError
from app.services.plans import PlanInfeasibleError, PlanStaleError, PlanStateError
from app.services.resume import ResumeError


@pytest.fixture
def client():
    return TestClient(create_app(Settings(_env_file=None, app_env="test")))


def _auth(client, user_id="user_one"):
    client.app.dependency_overrides[current_user] = lambda: UserResult(
        id=user_id, email=f"{user_id}@example.invalid", display_name=user_id
    )


def _document(**overrides):
    data = {
        "id": "doc_1",
        "kind": "resume",
        "filename": "resume.pdf",
        "current_version": 1,
        "status": "ready",
        "error": None,
    }
    data.update(overrides)
    return DocumentResult(**data)


def _execution(**overrides):
    data = {
        "id": "exec_1",
        "type": "analysis",
        "status": "running",
        "state_version": 2,
        "result_type": None,
        "result_id": None,
    }
    data.update(overrides)
    return ExecutionResult(**data)


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("DELETE", "/api/v1/documents/doc_1"),
        ("PUT", "/api/v1/documents/doc_1/text"),
        ("POST", "/api/v1/documents/doc_1/reindex"),
        ("GET", "/api/v1/executions/exec_1/events"),
        ("POST", "/api/v1/executions/exec_1/resume"),
        ("POST", "/api/v1/plans"),
        ("GET", "/api/v1/plans/plan_1"),
        ("POST", "/api/v1/plans/plan_1/confirm"),
        ("PATCH", "/api/v1/plans/plan_1/tasks/plan_1_t001"),
    ],
)
def test_m2_endpoints_require_authentication(client, method, url):
    response = client.request(method, url)
    assert response.status_code == 401


def test_delete_document_returns_202_with_purge_status(client, monkeypatch):
    _auth(client)
    monkeypatch.setattr(
        "app.api.routes.soft_delete_document",
        lambda settings, user_id, document_id: {"id": document_id, "status": "deleted"},
    )
    dispatched = {}
    monkeypatch.setattr(
        "app.api.routes.dispatch_purge_task",
        lambda settings, **kwargs: dispatched.update(kwargs),
    )
    response = client.delete("/api/v1/documents/doc_1")
    assert response.status_code == 202
    assert response.json()["data"] == {
        "document_id": "doc_1",
        "status": "deleted",
        "purge_status": "pending",
    }
    assert dispatched == {"user_id": "user_one", "document_id": "doc_1"}


def test_delete_document_unknown_or_foreign_returns_404(client, monkeypatch):
    _auth(client, "user_two")
    monkeypatch.setattr("app.api.routes.soft_delete_document", lambda *a: None)
    response = client.delete("/api/v1/documents/doc_1")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_update_document_text_creates_next_version(client, monkeypatch, tmp_path):
    _auth(client)
    monkeypatch.setattr(client.app.state.settings, "local_storage_dir", tmp_path / "storage")
    monkeypatch.setattr("app.api.routes.documents_get", lambda *a: _document())
    monkeypatch.setattr("app.api.routes.create_text_version", lambda settings, *a, **k: 2)
    monkeypatch.setattr("app.api.routes.create_execution", lambda *a, **k: "exec_new")
    dispatched = {}
    monkeypatch.setattr(
        "app.api.routes.dispatch_index_task",
        lambda settings, **kwargs: dispatched.update(kwargs),
    )
    response = client.put(
        "/api/v1/documents/doc_1/text",
        json={"extracted_text": "更新后的简历文本", "expected_version": 1},
    )
    assert response.status_code == 202
    assert response.json()["data"] == {
        "document_id": "doc_1",
        "version": 2,
        "execution_id": "exec_new",
    }
    assert dispatched["version"] == 2
    stored = (tmp_path / "storage" / dispatched["storage_key"]).read_text(encoding="utf-8")
    assert stored == "更新后的简历文本"


@pytest.mark.parametrize(
    ("document", "expected_code"),
    [
        (_document(status="failed"), "DOCUMENT_NOT_READY"),
        (_document(current_version=3), "VERSION_CONFLICT"),
    ],
)
def test_update_document_text_state_conflicts(client, monkeypatch, document, expected_code):
    _auth(client)
    monkeypatch.setattr("app.api.routes.documents_get", lambda *a: document)
    response = client.put(
        "/api/v1/documents/doc_1/text",
        json={"extracted_text": "text", "expected_version": 1},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == expected_code


def test_update_document_text_dispatch_failure_marks_version_failed(client, monkeypatch, tmp_path):
    _auth(client)
    monkeypatch.setattr(client.app.state.settings, "local_storage_dir", tmp_path / "storage")
    monkeypatch.setattr("app.api.routes.documents_get", lambda *a: _document())
    monkeypatch.setattr("app.api.routes.create_text_version", lambda *a, **k: 2)
    monkeypatch.setattr("app.api.routes.create_execution", lambda *a, **k: "exec_new")

    def broken(settings, **kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr("app.api.routes.dispatch_index_task", broken)
    monkeypatch.setattr("app.api.routes.transition_execution", lambda *a, **k: True)
    failed = {}
    monkeypatch.setattr(
        "app.api.routes.set_version_failed",
        lambda settings, user_id, document_id, version, **kwargs: failed.update(
            {"document_id": document_id, "version": version}
        ),
    )
    response = client.put(
        "/api/v1/documents/doc_1/text",
        json={"extracted_text": "text", "expected_version": 1},
    )
    assert response.status_code == 503
    assert failed == {"document_id": "doc_1", "version": 2}


def test_reindex_reuses_active_execution(client, monkeypatch):
    _auth(client)
    monkeypatch.setattr("app.api.routes.documents_get", lambda *a: _document())
    monkeypatch.setattr("app.api.routes.find_active_index_execution", lambda *a: "exec_active")
    response = client.post("/api/v1/documents/doc_1/reindex", json={"expected_version": 1})
    assert response.status_code == 202
    assert response.json()["data"]["execution_id"] == "exec_active"


def test_reindex_ready_version_is_rejected(client, monkeypatch):
    _auth(client)
    monkeypatch.setattr("app.api.routes.documents_get", lambda *a: _document())
    monkeypatch.setattr("app.api.routes.find_active_index_execution", lambda *a: None)
    monkeypatch.setattr("app.api.routes.get_version_row", lambda *a: {"status": "ready"})
    response = client.post("/api/v1/documents/doc_1/reindex", json={"expected_version": 1})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REINDEX_NOT_REQUIRED"


def test_reindex_failed_version_redispatches_with_source_key(client, monkeypatch):
    _auth(client)
    monkeypatch.setattr("app.api.routes.documents_get", lambda *a: _document())
    monkeypatch.setattr("app.api.routes.find_active_index_execution", lambda *a: None)
    monkeypatch.setattr(
        "app.api.routes.get_version_row",
        lambda *a: {"status": "failed", "source_key": "uploads/u/v1.pdf"},
    )
    monkeypatch.setattr("app.api.routes.create_execution", lambda *a, **k: "exec_retry")
    dispatched = {}
    monkeypatch.setattr(
        "app.api.routes.dispatch_index_task",
        lambda settings, **kwargs: dispatched.update(kwargs),
    )
    response = client.post("/api/v1/documents/doc_1/reindex", json={"expected_version": 1})
    assert response.status_code == 202
    assert dispatched["storage_key"] == "uploads/u/v1.pdf"
    assert dispatched["version"] == 1


def test_get_document_includes_extraction_preview(client, monkeypatch):
    _auth(client)
    monkeypatch.setattr("app.api.routes.documents_get", lambda *a: _document())
    monkeypatch.setattr("app.api.routes.recover_document_execution", lambda *a: None)
    monkeypatch.setattr(
        "app.api.routes.load_current_extraction",
        lambda *a: {"kind": "text", "text": "简历正文"},
    )
    response = client.get("/api/v1/documents/doc_1")
    assert response.status_code == 200
    extraction = response.json()["data"]["extraction"]
    assert extraction == {"kind": "text", "text": "简历正文", "pages": None}


def test_sse_conflicting_cursors_return_400(client, monkeypatch):
    _auth(client)
    monkeypatch.setattr("app.api.routes.executions_get", lambda *a: _execution())
    response = client.get(
        "/api/v1/executions/exec_1/events?after_event_id=ev_a",
        headers={"Last-Event-ID": "ev_b"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SSE_CURSOR_CONFLICT"


def test_sse_expired_cursor_returns_410(client, monkeypatch):
    _auth(client)
    monkeypatch.setattr("app.api.routes.executions_get", lambda *a: _execution())
    monkeypatch.setattr("app.api.routes.get_event_sequence", lambda *a: None)
    response = client.get(
        "/api/v1/executions/exec_1/events", headers={"Last-Event-ID": "ev_pruned"}
    )
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "EVENT_HISTORY_EXPIRED"


def test_sse_replays_after_cursor_and_closes_on_terminal_event(client, monkeypatch):
    _auth(client)
    monkeypatch.setattr("app.api.routes.executions_get", lambda *a: _execution())
    monkeypatch.setattr("app.api.routes.get_event_sequence", lambda *a: 1)
    rows = [
        {
            "id": "ev_2",
            "sequence": 2,
            "event_type": "stage",
            "status": "running",
            "payload": {"execution_id": "exec_1", "status": "running", "stage": "retrieving"},
        },
        {
            "id": "ev_3",
            "sequence": 3,
            "event_type": "completed",
            "status": "succeeded",
            "payload": {
                "execution_id": "exec_1",
                "status": "succeeded",
                "result_type": "analysis",
                "result_id": "analysis_1",
            },
        },
    ]
    monkeypatch.setattr("app.api.routes.list_events", lambda *a, **k: rows)
    with client.stream(
        "GET", "/api/v1/executions/exec_1/events", headers={"Last-Event-ID": "ev_1"}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        body = b"".join(response.iter_bytes()).decode("utf-8")
    assert "id: ev_2" in body
    assert "event: stage" in body
    assert "event: completed" in body
    assert '"result_id": "analysis_1"' in body


def test_sse_terminal_snapshot_fallback_when_history_gone(client, monkeypatch):
    _auth(client)
    monkeypatch.setattr(
        "app.api.routes.executions_get",
        lambda *a: _execution(
            status="succeeded",
            state_version=3,
            result_type="analysis",
            result_id="analysis_1",
        ),
    )
    monkeypatch.setattr("app.api.routes.list_events", lambda *a, **k: [])
    with client.stream("GET", "/api/v1/executions/exec_1/events") as response:
        body = b"".join(response.iter_bytes()).decode("utf-8")
    assert "event: completed" in body
    assert '"snapshot": true' in body


def test_resume_pins_ready_document_versions(client, monkeypatch):
    _auth(client)
    monkeypatch.setattr("app.api.routes.documents_get", lambda *a: _document())
    captured = {}
    monkeypatch.setattr(
        "app.api.routes.resume_execution_service",
        lambda settings, user_id, execution_id, **kwargs: captured.update(kwargs) or execution_id,
    )
    response = client.post(
        "/api/v1/executions/exec_1/resume",
        json={
            "interrupt_id": "int_1",
            "checkpoint_id": "chk_1",
            "answer": {"text": "", "document_ids": ["doc_1"]},
        },
    )
    assert response.status_code == 202
    assert captured["document_versions"] == [{"document_id": "doc_1", "version": 1}]


def test_resume_rejects_not_ready_document(client, monkeypatch):
    _auth(client)
    monkeypatch.setattr("app.api.routes.documents_get", lambda *a: _document(status="processing"))
    response = client.post(
        "/api/v1/executions/exec_1/resume",
        json={
            "interrupt_id": "int_1",
            "checkpoint_id": "chk_1",
            "answer": {"text": "", "document_ids": ["doc_1"]},
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_READY"


@pytest.mark.parametrize(
    ("raised", "status_code", "code"),
    [
        (ResumeError(404, "NOT_FOUND", "任务不存在。"), 404, "NOT_FOUND"),
        (ResumeError(409, "STALE_CHECKPOINT", "过期。"), 409, "STALE_CHECKPOINT"),
        (ResumeError(409, "NOT_WAITING", "不在等待。"), 409, "NOT_WAITING"),
    ],
)
def test_resume_error_mapping(client, monkeypatch, raised, status_code, code):
    _auth(client)

    def fail(settings, user_id, execution_id, **kwargs):
        raise raised

    monkeypatch.setattr("app.api.routes.resume_execution_service", fail)
    response = client.post(
        "/api/v1/executions/exec_1/resume",
        json={
            "interrupt_id": "int_1",
            "checkpoint_id": "chk_1",
            "answer": {"text": "补充内容"},
        },
    )
    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code


def test_resume_input_conflict_maps_to_409(client, monkeypatch):
    _auth(client)

    def fail(settings, user_id, execution_id, **kwargs):
        raise InputConflictError("int_1")

    monkeypatch.setattr("app.api.routes.resume_execution_service", fail)
    response = client.post(
        "/api/v1/executions/exec_1/resume",
        json={
            "interrupt_id": "int_1",
            "checkpoint_id": "chk_1",
            "answer": {"text": "补充内容"},
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INPUT_CONFLICT"


def test_create_plan_commits_execution_and_outbox_in_transaction(client, monkeypatch):
    _auth(client)
    monkeypatch.setattr(
        "app.api.routes.analyses_get",
        lambda *a: {"id": "analysis_1", "status": "ready", "job_id": "job_1"},
    )
    monkeypatch.setattr("app.api.routes.analysis_is_stale", lambda *a: False)
    connection = MagicMock()
    connection.return_value.__enter__.return_value = (MagicMock(), MagicMock())
    monkeypatch.setattr("app.api.routes.transactional_connection", connection)
    monkeypatch.setattr("app.api.routes.create_plan_record", lambda *a, **k: "plan_1")
    monkeypatch.setattr("app.api.routes.insert_execution", lambda *a, **k: "exec_plan")
    monkeypatch.setattr("app.api.routes.attach_plan_execution", lambda *a, **k: None)
    monkeypatch.setattr("app.api.routes.enqueue_dispatch", lambda *a, **k: "disp_1")
    published = []
    monkeypatch.setattr(
        "app.api.routes.publish_dispatch",
        lambda settings, dispatch_id: published.append(dispatch_id) or True,
    )
    response = client.post(
        "/api/v1/plans",
        json={
            "analysis_id": "analysis_1",
            "deadline": "2099-01-01",
            "timezone": "Asia/Shanghai",
            "daily_minutes": 60,
        },
    )
    assert response.status_code == 202
    assert response.json()["data"] == {"execution_id": "exec_plan"}
    assert published == ["disp_1"]


@pytest.mark.parametrize(
    ("analysis", "stale", "status_code", "code"),
    [
        (None, False, 404, "NOT_FOUND"),
        ({"id": "a", "status": "failed", "error": "失败"}, False, 409, "ANALYSIS_FAILED"),
        ({"id": "a", "status": "pending"}, False, 409, "ANALYSIS_NOT_READY"),
        ({"id": "a", "status": "ready", "job_id": "j"}, True, 409, "ANALYSIS_STALE"),
    ],
)
def test_create_plan_source_validation(client, monkeypatch, analysis, stale, status_code, code):
    _auth(client)
    monkeypatch.setattr("app.api.routes.analyses_get", lambda *a: analysis)
    monkeypatch.setattr("app.api.routes.analysis_is_stale", lambda *a: stale)
    response = client.post(
        "/api/v1/plans",
        json={
            "analysis_id": "analysis_1",
            "deadline": "2099-01-01",
            "timezone": "Asia/Shanghai",
            "daily_minutes": 60,
        },
    )
    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize(
    ("payload_override", "code"),
    [
        ({"timezone": "Not/AZone"}, "INVALID_INPUT"),
        ({"deadline": "2000-01-01"}, "PLAN_INFEASIBLE"),
    ],
)
def test_create_plan_constraint_validation(client, monkeypatch, payload_override, code):
    _auth(client)
    payload = {
        "analysis_id": "analysis_1",
        "deadline": "2099-01-01",
        "timezone": "Asia/Shanghai",
        "daily_minutes": 60,
    }
    payload.update(payload_override)
    response = client.post("/api/v1/plans", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code


def _plan_row(**overrides):
    row = {
        "id": "plan_1",
        "user_id": "user_one",
        "analysis_id": "analysis_1",
        "execution_id": "exec_1",
        "deadline": date(2099, 1, 1).isoformat(),
        "timezone": "Asia/Shanghai",
        "daily_minutes": 60,
        "status": "draft",
        "draft_tasks": [
            {
                "title": "复习索引",
                "requirement_ids": ["req_01"],
                "scheduled_date": date(2099, 1, 1).isoformat(),
                "estimated_minutes": 45,
                "source_refs": [],
            }
        ],
        "confirmed_at": None,
        "created_at": datetime.now(UTC),
    }
    row.update(overrides)
    return row


def test_get_plan_returns_draft(client, monkeypatch):
    _auth(client)
    monkeypatch.setattr("app.api.routes.get_plan", lambda *a: _plan_row())
    response = client.get("/api/v1/plans/plan_1")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "draft"
    assert data["draft_tasks"][0]["title"] == "复习索引"
    assert data["tasks"] is None


def test_get_plan_other_user_returns_404(client, monkeypatch):
    _auth(client, "user_two")
    monkeypatch.setattr("app.api.routes.get_plan", lambda *a: None)
    assert client.get("/api/v1/plans/plan_1").status_code == 404


def test_confirm_plan_returns_confirmed_result(client, monkeypatch):
    _auth(client)
    confirmed = _plan_row(status="confirmed", confirmed_at=datetime.now(UTC))
    monkeypatch.setattr(
        "app.api.routes.confirm_plan",
        lambda settings, user_id, plan_id, **kwargs: {
            "plan": confirmed,
            "tasks": [
                {
                    "id": "plan_1_t001",
                    "plan_id": "plan_1",
                    "title": "复习索引",
                    "requirement_ids": ["req_01"],
                    "scheduled_date": date(2099, 1, 1).isoformat(),
                    "estimated_minutes": 45,
                    "status": "todo",
                    "source_refs": [],
                }
            ],
            "execution_id": "exec_1",
            "resumed": True,
        },
    )
    response = client.post("/api/v1/plans/plan_1/confirm")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["resumed"] is True
    assert data["execution_id"] == "exec_1"
    assert data["plan"]["status"] == "confirmed"
    assert data["plan"]["tasks"][0]["id"] == "plan_1_t001"


@pytest.mark.parametrize(
    ("raised", "status_code", "code"),
    [
        (PlanStaleError("analysis_1"), 409, "ANALYSIS_STALE"),
        (PlanInfeasibleError(["超出截止日期"]), 422, "PLAN_INFEASIBLE"),
        (PlanStateError("PLAN_NOT_FOUND", "不存在。"), 404, "NOT_FOUND"),
        (PlanStateError("PLAN_DRAFT_NOT_READY", "草稿未生成。"), 409, "PLAN_DRAFT_NOT_READY"),
        (InputConflictError("int_1"), 409, "INPUT_CONFLICT"),
    ],
)
def test_confirm_plan_error_mapping(client, monkeypatch, raised, status_code, code):
    _auth(client)

    def fail(settings, user_id, plan_id, **kwargs):
        raise raised

    monkeypatch.setattr("app.api.routes.confirm_plan", fail)
    response = client.post("/api/v1/plans/plan_1/confirm")
    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code


def test_update_plan_task_status(client, monkeypatch):
    _auth(client)
    monkeypatch.setattr(
        "app.api.routes.update_plan_task_status",
        lambda *a, **k: {
            "id": "plan_1_t001",
            "plan_id": "plan_1",
            "title": "复习索引",
            "requirement_ids": [],
            "scheduled_date": date(2099, 1, 1).isoformat(),
            "estimated_minutes": 45,
            "status": "done",
            "source_refs": [],
        },
    )
    response = client.patch("/api/v1/plans/plan_1/tasks/plan_1_t001", json={"status": "done"})
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "done"
    monkeypatch.setattr("app.api.routes.update_plan_task_status", lambda *a, **k: None)
    assert (
        client.patch("/api/v1/plans/plan_1/tasks/plan_1_t001", json={"status": "done"}).status_code
        == 404
    )
