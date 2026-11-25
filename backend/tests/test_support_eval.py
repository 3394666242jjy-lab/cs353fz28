"""证据充分性评测指标的单元测试（成员 C）。

判定模型与向量服务都不参与：这里只锁指标语义——分母是谁、未判定怎么处理、
引用失效怎么算。真实调用与离线替身的边界见 dev/18。
"""

import pytest
from pydantic import ValidationError

from app.rag.support_eval import (
    SUPPORT_METRIC_VERSION,
    AssembledContext,
    EvidenceAnchor,
    SupportItem,
    SupportJudgement,
    SupportResult,
    assemble_context,
    citation_precision,
    citation_recall,
    confusion_matrix,
    false_support_rate,
    over_refusal_rate,
    render_comparison,
    render_markdown,
    resolution_note,
    resolve_anchors,
    stale_citation_rate,
    status_accuracy,
    summarize,
)
from app.schemas.analysis import DocumentVersion


def result(
    *,
    axis: str = "fully_supported",
    expected: str = "supported",
    judged: str | None = "supported",
    cited: tuple[str, ...] = (),
    expected_evidence: tuple[str, ...] = (),
    forbidden: tuple[str, ...] = (),
    context_chars: int = 100,
    dropped_blocks: int = 0,
    total_ms: float = 10.0,
) -> SupportResult:
    """构造一条评测结果；未判定用 judged=None 表达。"""
    return SupportResult(
        item_id="i",
        axis=axis,
        expected_status=expected,
        judged_status=judged,
        cited=cited,
        expected_evidence=expected_evidence,
        forbidden=forbidden,
        context_chars=context_chars,
        context_blocks=3,
        dropped_blocks=dropped_blocks,
        total_ms=total_ms,
    )


def anchor(text: str, *, document_id: str = "doc_a", version: int = 1) -> EvidenceAnchor:
    return EvidenceAnchor(document_id=document_id, version=version, anchor=text)


def test_resolve_anchors_requires_exactly_one_hit():
    """锚点必须唯一命中，否则标注指向不明。"""
    chunks = {("doc_a", 1): {"c1": "第一段：缓存命中率 92%", "c2": "第二段：吞吐 1200"}}

    assert resolve_anchors([anchor("缓存命中率 92%")], chunks) == ("c1",)


def test_resolve_anchors_fails_loudly_when_corpus_drifts():
    """语料或切分变了要显式失败——标注不能悄悄指向别处。"""
    chunks = {("doc_a", 1): {"c1": "完全不同的内容"}}

    with pytest.raises(ValueError, match="锚点未命中任何 chunk"):
        resolve_anchors([anchor("缓存命中率 92%")], chunks)


def test_resolve_anchors_rejects_ambiguous_and_unknown_scopes():
    """锚点太宽会命中多个 chunk；版本不存在同样要报错。"""
    chunks = {("doc_a", 1): {"c1": "重复描述", "c2": "重复描述"}}

    with pytest.raises(ValueError, match="命中 2 个 chunk"):
        resolve_anchors([anchor("重复描述")], chunks)
    with pytest.raises(ValueError, match="不在语料里"):
        resolve_anchors([anchor("重复描述", version=2)], chunks)


def test_resolve_anchors_rejects_duplicate_chunk_labels():
    """同一条要求的两处证据解析到同一个 chunk，说明标注写重了。"""
    chunks = {("doc_a", 1): {"c1": "同一片段里有甲乙两句话"}}

    with pytest.raises(ValueError, match="重复标注"):
        resolve_anchors([anchor("同一片段里"), anchor("甲乙两句话")], chunks)


def test_assemble_context_keeps_given_order_and_reports_dropped_blocks():
    """装不下就整块跳过并记录下来，不能静默截断成半块。"""
    blocks = [("c2", "乙" * 10), ("c1", "甲" * 10), ("c3", "丙" * 10)]

    context = assemble_context(blocks, max_chars=25)

    assert isinstance(context, AssembledContext)
    assert context.block_ids == ("c2", "c1")
    assert context.dropped_ids == ("c3",)
    assert context.truncated is True
    assert "[c2]" in context.text and "[c3]" not in context.text


def test_assemble_context_is_not_truncated_when_everything_fits():
    context = assemble_context([("c1", "短片段")], max_chars=100)

    assert context.dropped_ids == ()
    assert context.truncated is False
    assert context.chars == len(context.text)


def test_false_support_rate_counts_only_risky_items():
    """错误支持率的分母是"期望不是 supported"的题，判成 supported 才算错。"""
    results = [
        result(expected="supported", judged="supported"),
        result(expected="partial", judged="supported"),
        result(expected="not_evidenced", judged="supported"),
        result(expected="partial", judged="partial"),
    ]

    assert false_support_rate(results) == pytest.approx(2 / 3)


def test_rates_return_none_without_samples_instead_of_zero():
    """没有样本时返回 None：0 会被读成"没有这个错误"。"""
    only_supported = [result(expected="supported", judged="supported")]

    assert false_support_rate(only_supported) is None
    assert over_refusal_rate([result(expected="partial", judged="partial")]) is None
    assert status_accuracy([]) is None


def test_over_refusal_rate_is_the_opposite_failure():
    """把撑得住的判成无证据，同样是把有依据的经历否掉。"""
    results = [
        result(expected="supported", judged="not_evidenced"),
        result(expected="supported", judged="supported"),
        result(expected="partial", judged="not_evidenced"),
    ]

    assert over_refusal_rate(results) == pytest.approx(1 / 2)


def test_stale_citation_is_counted_separately():
    """引用旧版本或已删除片段比不引用更糟，单独统计。"""
    results = [
        result(expected="not_evidenced", judged="partial", cited=("old",), forbidden=("old",)),
        result(expected="not_evidenced", judged="partial", cited=("new",), forbidden=("old",)),
        result(expected="supported", judged="supported", cited=("x",)),
    ]

    assert stale_citation_rate(results) == pytest.approx(1 / 2)


def test_citation_precision_and_recall_use_expected_evidence():
    """引用正确率与召回都只对照标注证据；部分支持也要算。"""
    results = [
        result(
            expected="partial",
            judged="partial",
            cited=("a", "z"),
            expected_evidence=("a", "b"),
        )
    ]

    assert citation_precision(results) == pytest.approx(1 / 2)
    assert citation_recall(results) == pytest.approx(1 / 2)


def test_citation_metrics_ignore_not_evidenced_judgements():
    """判成无证据的题不该被算成"引用错"，那是另一条路径的错误。"""
    results = [result(expected="supported", judged="not_evidenced")]

    assert citation_precision(results) is None
    assert citation_recall(results) is None


def test_undecided_results_never_enter_any_ratio():
    """judge 没跑就不产出比率——不折算成 0，也不折算成正确。"""
    results = [
        result(expected="partial", judged=None),
        result(expected="not_evidenced", judged="not_evidenced"),
    ]

    summary = summarize(results, arm="full-context")

    assert summary["counts"] == {
        "total": 2,
        "decided": 1,
        "undecided": 1,
        "by_axis": {"fully_supported": {"count": 2, "decided": 1, "false_support": 0}},
    }
    assert summary["false_support_rate"] == 0.0
    assert summary["metric_version"] == SUPPORT_METRIC_VERSION


def test_confusion_matrix_exposes_three_state_moves():
    """三态之间的每次挪动都要能看到，而不只是一个对/错。"""
    results = [
        result(expected="supported", judged="partial"),
        result(expected="partial", judged="supported"),
    ]

    matrix = confusion_matrix(results)

    assert matrix["supported"]["partial"] == 1
    assert matrix["partial"]["supported"] == 1
    assert matrix["not_evidenced"] == {"supported": 0, "partial": 0, "not_evidenced": 0}


def test_summarize_reports_context_volume_and_truncation():
    """上下文体积是对照臂的核心证据：全资料赢的时候要能看到它贵多少。"""
    results = [
        result(context_chars=200),
        result(context_chars=400, dropped_blocks=2),
    ]

    summary = summarize(results, arm="full-context")

    assert summary["context"]["mean_chars"] == 300.0
    assert summary["context"]["max_chars"] == 400
    assert summary["context"]["truncated_items"] == 1


def test_render_markdown_states_samples_and_judge_version():
    """报告必须同时给出样本分辨率与判定模型版本，否则数字无法复核。"""
    summary = summarize([result(expected="partial", judged="partial")], arm="dense")

    text = render_markdown(summary, judge_version="qwen3.7-plus")

    assert "qwen3.7-plus" in text
    assert SUPPORT_METRIC_VERSION in text
    assert "无样本" in text
    assert "1 条 = 1.0000" in text


def test_render_comparison_puts_arms_side_by_side():
    """单臂数字看不出 RAG 的收益，必须并排。"""
    topk = summarize([result(context_chars=500, dropped_blocks=0)], arm="dense")
    full = summarize([result(context_chars=4000, dropped_blocks=3)], arm="full-context")

    text = render_comparison([topk, full], notes=["小语料下全文可容纳，结论不可外推"])

    assert "`dense`" in text and "`full-context`" in text
    assert "| 平均上下文（字） | 500 | 4000 |" in text
    assert "不可外推" in text


def test_resolution_note_scales_with_sample_size():
    """样本越少分辨率越粗，这一句要跟数字一起出现在报告里。"""
    assert resolution_note(0).startswith("无可判定样本")
    assert "1.0000" in resolution_note(1)
    assert "0.0833" in resolution_note(12)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"status": "not_evidenced", "cited_chunk_ids": ["c1"]}, "不得引用证据"),
        ({"status": "supported", "cited_chunk_ids": []}, "必须引用证据"),
        (
            {"status": "partial", "cited_chunk_ids": ["c1"], "missing_information": []},
            "必须说明缺什么",
        ),
        (
            {"status": "supported", "cited_chunk_ids": ["c1"], "missing_information": ["缺"]},
            "不应有未解决的缺失说明",
        ),
        (
            {"status": "partial", "cited_chunk_ids": ["c1", "c1"], "missing_information": ["缺"]},
            "不得重复",
        ),
    ],
)
def test_judgement_validators_mirror_production_contract(payload, message):
    """判定约束必须与生产的 Assessment 一致，否则评测口径与线上会分叉。"""
    with pytest.raises(ValidationError, match=message):
        SupportJudgement.model_validate(payload)


def test_item_requires_evidence_only_when_expectation_demands_it():
    """标注自相矛盾（无证据却给了证据 / 有证据却没标注）应当直接拒绝。"""
    base = {
        "id": "s1",
        "axis": "partially_supported",
        "user_id": "u1",
        "requirement": "要求",
        "document_versions": [{"document_id": "doc_a", "version": 1}],
        "note": "依据",
    }

    with pytest.raises(ValidationError, match="必须标注期望证据"):
        SupportItem.model_validate({**base, "expected_status": "partial"})
    with pytest.raises(ValidationError, match="不应标注期望证据"):
        SupportItem.model_validate(
            {
                **base,
                "axis": "no_evidence",
                "expected_status": "not_evidenced",
                "expected_evidence": [{"document_id": "doc_a", "version": 1, "anchor": "某段原文"}],
            }
        )

    item = SupportItem.model_validate(
        {
            **base,
            "expected_status": "partial",
            "expected_evidence": [{"document_id": "doc_a", "version": 1, "anchor": "某段原文"}],
        }
    )
    assert item.document_versions == [DocumentVersion(document_id="doc_a", version=1)]
