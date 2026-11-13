import json
from pathlib import Path

import pytest

from app.agent.knowledge_policy import compose_context, route_retrieval, validate_personal_citations
from app.rag.knowledge import load_catalog, retrieve_local_knowledge
from app.rag.structured_chunking import (
    ChunkingLimitError,
    ChunkPolicy,
    normalize_source,
    split_structured,
)
from app.schemas.analysis import Evidence, EvidenceRef, TextLocator

ROOT = Path(__file__).resolve().parents[2] / "knowledge"


def split(text, size=128):
    return split_structured(
        text, source_id="synthetic", source_version="v1", policy=ChunkPolicy(size, 1024)
    )


def test_structured_chunks_are_exact_and_cover_every_non_whitespace_character():
    text = (
        "# Root\r\n\r\n## First\r\n"
        + "A long paragraph with context. " * 30
        + "\r\n\r\n## Second\r\nOther text.\r\n"
    )
    normalized = normalize_source(text)
    covered = set()
    chunks = split(text)
    for chunk in chunks:
        assert chunk.text == normalized[chunk.char_start : chunk.char_end]
        assert chunk.parent_start <= chunk.char_start < chunk.char_end <= chunk.parent_end
        assert chunk.line_start <= chunk.line_end
        covered.update(range(chunk.char_start, chunk.char_end))
    assert all(i in covered for i, char in enumerate(normalized) if not char.isspace())
    assert len(covered) == sum(chunk.char_end - chunk.char_start for chunk in chunks)
    assert chunks[-1].heading_path == ("Root", "Second")


def test_code_fences_and_tables_stay_intact_even_over_target():
    code = "```python\n\n# not a heading\n" + "print('synthetic')\n" * 12 + "```\n"
    table = (
        "| Metric | Meaning |\n| --- | --- |\n" + "| success | verified task completion |\n" * 10
    )
    text = f"# Guide\n\n{code}\n## Metrics\n\n{table}"
    chunks = split(text)
    assert any(code in chunk.text and "code" in chunk.kinds for chunk in chunks)
    assert any(table in chunk.text and "table" in chunk.kinds for chunk in chunks)
    assert all("not a heading" not in chunk.heading_path for chunk in chunks)


def test_oversized_atomic_block_fails_explicitly():
    with pytest.raises(ChunkingLimitError):
        split("```\n" + "x" * 1100 + "\n```\n")


def test_ids_pin_source_version_content_and_policy():
    text = "# Topic\n\n" + "word " * 80
    first = split(text)
    assert first == split(text)
    changed = split_structured(
        text, source_id="synthetic", source_version="v2", policy=ChunkPolicy(128, 1024)
    )
    assert not {c.id for c in first} & {c.id for c in changed}
    assert first[0].id != split(text, 256)[0].id
    assert first[0].text in first[0].embedding_text


@pytest.mark.parametrize("size,hard", [(0, 100), (200, 100), (100, 100000)])
def test_invalid_policies_are_rejected(size, hard):
    with pytest.raises(ValueError):
        ChunkPolicy(size, hard)


def test_empty_source_and_long_line_offsets():
    assert split(" \n\n") == []
    chunks = split("x" * 900)
    assert all(len(c.text) <= 128 and c.line_start == c.line_end == 1 for c in chunks)
    assert "".join(c.text for c in chunks) == "x" * 900


def test_catalog_is_pinned_and_all_three_roles_have_sources():
    catalog = load_catalog(ROOT)
    assert len(catalog) == 13
    for role in ("ai_product_manager", "fullstack", "agent_engineer"):
        assert sum(role in source.roles for source, _ in catalog) >= 4
    assert all(source.usage == "reference_only" for source, _ in catalog)


def test_catalog_rejects_content_changes_and_path_escape(tmp_path):
    registry = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    registry["sources"] = [registry["sources"][0]]
    source = registry["sources"][0]
    source["local_path"] = "card.md"
    (tmp_path / "card.md").write_text("unreviewed change", encoding="utf-8")
    manifest = tmp_path / "sources.json"
    manifest.write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint"):
        load_catalog(tmp_path)
    source["local_path"] = "../outside.md"
    manifest.write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises(ValueError, match="escapes"):
        load_catalog(tmp_path)


def test_role_filter_and_no_hit_do_not_invent_references():
    refs = retrieve_local_knowledge(ROOT, role="fullstack", query="React 组件 状态")
    assert refs and refs[0].source_id == "full-react"
    assert all("fullstack" in ref.roles for ref in refs)
    assert retrieve_local_knowledge(ROOT, role="ai_product_manager", query="zqxyz987652") == []


def test_public_knowledge_never_proves_personal_experience():
    evidence = Evidence(
        document_id="public_doc",
        version=1,
        chunk_id="c",
        filename="public.md",
        source_type="public",
        quote="knowledge",
        locator=TextLocator(kind="text", line_start=1, line_end=1),
    )
    ref = EvidenceRef(document_id="public_doc", version=1, chunk_id="c")
    with pytest.raises(ValueError):
        validate_personal_citations([ref], [evidence])
    with pytest.raises(ValueError):
        compose_context(
            route_retrieval("agent_engineer", "prepare_for_role"), personal=[evidence], knowledge=[]
        )


def test_intent_routes_and_context_budget_are_enforced():
    knowledge = retrieve_local_knowledge(ROOT, role="fullstack", query="React 组件")
    plan = route_retrieval("fullstack", "learn_topic")
    context = compose_context(plan, personal=[], knowledge=knowledge)
    assert context.reference_knowledge and not context.personal_evidence
    with pytest.raises(ValueError):
        compose_context(
            route_retrieval("fullstack", "match_personal_experience"),
            personal=[],
            knowledge=knowledge,
        )
    with pytest.raises(ValueError):
        compose_context(
            route_retrieval("agent_engineer", "learn_topic"), personal=[], knowledge=knowledge
        )
    with pytest.raises(ValueError):
        compose_context(
            plan.model_copy(update={"max_reference_chars": 1}), personal=[], knowledge=knowledge
        )
    empty = compose_context(
        route_retrieval("agent_engineer", "prepare_for_role"), personal=[], knowledge=[]
    )
    assert empty.missing_information
