import copy
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.schemas.analysis import AnalysisCreate, AnalysisResult, Evidence, Match, TextLocator
from app.schemas.common import Envelope
from app.schemas.resources import ExecutionResult, LoginRequest

ROOT = Path(__file__).resolve().parents[2]


def fixture(name):
    return json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))


def test_example_responses_match_runtime_contract_and_real_source_locations():
    analysis = Envelope[AnalysisResult].model_validate(fixture("analysis-response.json")).data
    execution = Envelope[ExecutionResult].model_validate(fixture("execution-response.json")).data
    assert execution.result_id == analysis.id
    evidence = {}
    for response in fixture("evidence-responses.json")["responses"].values():
        item = Envelope[Evidence].model_validate(response).data
        evidence[item.chunk_id] = item
        lines = (ROOT / "examples" / item.filename).read_text(encoding="utf-8").splitlines()
        assert isinstance(item.locator, TextLocator)
        assert item.quote == "\n".join(lines[item.locator.line_start - 1 : item.locator.line_end])
    for match in analysis.matches:
        for ref in match.evidence_refs:
            assert evidence[ref.chunk_id].document_id == ref.document_id


@pytest.mark.parametrize("status", ["supported", "partial"])
def test_positive_matches_require_evidence(status):
    match = copy.deepcopy(fixture("analysis-response.json")["data"]["matches"][0])
    match.update(status=status, evidence_refs=[])
    with pytest.raises(ValidationError):
        Match.model_validate(match)


def test_no_evidence_match_cannot_reference_another_document():
    analysis = fixture("analysis-response.json")["data"]
    analysis["matches"][0]["evidence_refs"][0]["document_id"] = "another-user-document"
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(analysis)


def test_invalid_source_ranges_and_duplicate_document_ids_are_rejected():
    with pytest.raises(ValidationError):
        TextLocator(kind="text", line_start=16, line_end=11)
    with pytest.raises(ValidationError):
        AnalysisCreate(job_id="job_1", document_ids=["doc_1", "doc_1"])


def test_password_is_not_silently_trimmed():
    assert LoginRequest(email="demo@example.invalid", password=" spaced ").password == " spaced "


def test_local_compose_binds_only_loopback_and_uses_persistent_volumes():
    config = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert set(config["services"]) == {"mongodb", "redis", "qdrant"}
    for service in config["services"].values():
        assert all(port.startswith("127.0.0.1:") for port in service["ports"])
        assert service["volumes"]
