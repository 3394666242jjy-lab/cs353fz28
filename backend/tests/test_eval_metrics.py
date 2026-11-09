"""M2 检索评测指标的单元测试（成员 C，issue #25）。"""

from app.rag.eval_metrics import (
    QueryResult,
    citation_precision_at_k,
    is_violation,
    percentile,
    recall_at_k,
    reciprocal_rank,
    render_markdown,
    summarize,
)


def answerable(returned: list[str], relevant: list[str]) -> QueryResult:
    """构造一条可回答问题结果；耗时字段不影响指标断言。"""
    return QueryResult(
        question_id="q",
        kind="answerable",
        expect=None,
        relevant=tuple(relevant),
        forbidden=(),
        returned=tuple(returned),
        total_ms=10.0,
        embedding_ms=6.0,
        search_ms=4.0,
    )


def negative(kind: str, returned: list[str], *, expect: str = "empty", forbidden=()) -> QueryResult:
    """构造一条负例结果。"""
    return QueryResult(
        question_id="n",
        kind=kind,
        expect=expect,
        relevant=(),
        forbidden=tuple(forbidden),
        returned=tuple(returned),
        total_ms=10.0,
        embedding_ms=6.0,
        search_ms=4.0,
    )


def test_recall_at_k_counts_covered_labels():
    """Recall@k 只看前 k 个结果是否覆盖标注。"""
    result = answerable(["a", "b", "c"], ["c", "x"])

    assert recall_at_k(result, 1) == 0.0
    assert recall_at_k(result, 3) == 0.5


def test_recall_without_labels_is_zero_not_one():
    """没有标注时不应算出满分，否则负例混入会虚高。"""
    assert recall_at_k(answerable(["a"], []), 5) == 0.0


def test_reciprocal_rank_uses_first_relevant_position():
    """MRR 取首个相关结果的倒数排名。"""
    assert reciprocal_rank(answerable(["a", "b", "c"], ["b"])) == 0.5
    assert reciprocal_rank(answerable(["a", "b"], ["z"])) == 0.0


def test_citation_precision_penalises_padding():
    """把候选堆满会让引用正确性下降——这与 Recall 互补，防止刷分。"""
    precise = answerable(["a", "b"], ["a"])
    padded = answerable(["a", "x", "y", "z"], ["a"])

    assert citation_precision_at_k(precise, 5) == 0.5
    assert citation_precision_at_k(padded, 5) == 0.25
    assert citation_precision_at_k(answerable([], ["a"]), 5) == 0.0


def test_empty_negative_violates_only_when_something_is_returned():
    """empty 负例：只要返回了任何结果就算违规。"""
    assert is_violation(negative("no_evidence", [])) is False
    assert is_violation(negative("no_evidence", ["a"])) is True


def test_old_version_negative_only_forbids_the_stale_chunk():
    """旧版本负例：返回语义相近的新版本 chunk 是正确行为，不算违规。"""
    stale = negative("old_version", ["new_chunk"], expect="forbidden_absent", forbidden=["old"])

    assert is_violation(stale) is False
    assert (
        is_violation(negative("old_version", ["old"], expect="forbidden_absent", forbidden=["old"]))
        is True
    )


def test_summarize_separates_negatives_and_marks_deleted_pending():
    """负例单独统计；deleted 在清理链路就绪前标记为未判定。"""
    results = [
        answerable(["a"], ["a"]),
        negative("no_evidence", []),
        negative("deleted", ["a"]),
    ]

    summary = summarize(results)

    assert summary["answerable"]["count"] == 1
    assert summary["answerable"]["recall_at_1"] == 1.0
    assert summary["negatives"]["no_evidence"] == {"count": 1, "violations": 0, "rate": 0.0}
    assert summary["negatives"]["deleted"]["pending"] is True
    assert summary["negatives"]["deleted"]["violations"] == 1


def test_percentile_uses_nearest_rank_and_handles_empty():
    """样本很少时刻意不插值，避免制造精度幻觉。"""
    assert percentile([], 0.95) == 0.0
    assert percentile([10.0], 0.95) == 10.0
    assert percentile([1.0, 2.0, 3.0], 0.50) == 2.0


def test_render_markdown_states_pending_instead_of_fake_rate():
    """未判定的负例在报告里必须写明，不能显示成一个好看的数字。"""
    results = [
        answerable(["a"], ["a"]),
        negative("deleted", []),
    ]

    text = render_markdown(summarize(results), strategy="dense", config_version="dense/m@64/top5")

    assert "dense/m@64/top5" in text
    assert "未判定" in text
