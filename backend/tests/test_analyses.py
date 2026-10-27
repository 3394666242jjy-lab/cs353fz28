"""Tests for the owner-scoped analyses data layer (issue #7)."""

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.schemas.analysis import AnalysisResult, DocumentVersion
from app.services.analyses import (
    create_analysis,
    get_analysis,
    requirement_snapshot_from_matches,
    save_analysis_result,
    set_analysis_failed,
)

RESULT = AnalysisResult(
    id="analysis_1",
    job_id="job_1",
    job_version=2,
    document_versions=[DocumentVersion(document_id="doc_x", version=3)],
    stale=False,
    matches=[
        {
            "requirement_id": "req_1",
            "requirement": "Python",
            "category": "required",
            "status": "supported",
            "evidence_refs": [{"chunk_id": "c1", "document_id": "doc_x", "version": 3}],
            "explanation": "built services in Python",
            "missing_information": [],
        }
    ],
    warnings=[],
    trace_id="trace_1",
    prompt_version="prompt-1",
    retrieval_config_version="retrieval-1",
    model_version="model-1",
)


@pytest.fixture
def settings():
    return Settings(_env_file=None, app_env="test")


@pytest.fixture
def database(monkeypatch):
    connection = MagicMock()
    monkeypatch.setattr("app.services.analyses.database_connection", connection)
    return connection.return_value.__enter__.return_value


def test_create_analysis_pins_versions_and_stays_pending(settings, database):
    analysis_id = create_analysis(
        settings,
        "user_one",
        job_id="job_1",
        job_version=2,
        document_versions=[DocumentVersion(document_id="doc_x", version=3)],
    )
    assert analysis_id.startswith("analysis_")
    row = database.analyses.insert_one.call_args.args[0]
    assert row["status"] == "pending"
    assert row["job_version"] == 2
    assert row["document_versions"] == [{"document_id": "doc_x", "version": 3}]
    assert row["matches"] == []
    assert row["stale"] is False


def test_get_analysis_scopes_by_owner(settings, database):
    database.analyses.find_one.return_value = {"id": "analysis_1"}
    assert get_analysis(settings, "user_one", "analysis_1") == {"id": "analysis_1"}
    query = database.analyses.find_one.call_args.args[0]
    assert query == {"id": "analysis_1", "user_id": "user_one"}


def test_save_analysis_result_only_from_pending(settings, database):
    database.analyses.update_one.return_value.matched_count = 1
    assert save_analysis_result(settings, "user_one", "analysis_1", RESULT) is True
    query = database.analyses.update_one.call_args.args[0]
    assert query == {"id": "analysis_1", "user_id": "user_one", "status": "pending"}
    update = database.analyses.update_one.call_args.args[1]["$set"]
    assert update["status"] == "ready"
    assert update["job_version"] == 2
    assert update["matches"][0]["requirement_id"] == "req_1"
    assert update["trace_id"] == "trace_1"
    assert update["retrieval_config_version"] == "retrieval-1"


def test_save_analysis_result_reports_concurrent_update(settings, database):
    database.analyses.update_one.return_value.matched_count = 0
    assert save_analysis_result(settings, "user_one", "analysis_1", RESULT) is False


def test_save_analysis_result_rejects_invalid_contract(settings, database):
    with pytest.raises(ValidationError):
        save_analysis_result(
            settings, "user_one", "analysis_1", {"id": "analysis_1", "job_id": "job_1"}
        )


def test_set_analysis_failed_keeps_owner_scope(settings, database):
    database.analyses.update_one.return_value.matched_count = 1
    assert set_analysis_failed(settings, "user_one", "analysis_1", error="boom") is True
    query = database.analyses.update_one.call_args.args[0]
    assert query["status"] == "pending"
    update = database.analyses.update_one.call_args.args[1]["$set"]
    assert update["status"] == "failed"
    assert update["error"] == "boom"


def test_requirement_snapshot_derives_from_matches():
    snapshot = requirement_snapshot_from_matches(RESULT.matches)
    assert snapshot == [{"id": "req_1", "text": "Python", "category": "required"}]
