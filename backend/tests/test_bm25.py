"""BM25 稀疏编码的单元测试（RAG 模块，成员 C，issue #25）。"""

import math

import pytest

from app.rag.bm25 import (
    Bm25Encoder,
    SparseVector,
    build_idf,
    term_index,
    tokenize,
)


def test_tokenize_splits_ascii_words_and_cjk_bigrams():
    """英文数字按词切分，汉字按 bigram 切分，单字保留单字。"""
    tokens = tokenize("缓存命中率 92%，FastAPI 0.115.0，猫")

    assert "fastapi" in tokens
    assert "0.115.0" in tokens
    assert "缓存" in tokens and "命中" in tokens
    assert "猫" in tokens  # 单字成词时保留


def test_tokenize_keeps_literal_numbers_that_semantic_search_misses():
    """字面 token 必须能被切出来——这正是混合检索补 dense 的地方。"""
    assert "1200" in tokenize("压测每秒约 1200 次请求")
    assert "8.0.36" in tokenize("MySQL 8.0.36")


def test_term_index_is_stable_and_in_uint32_range():
    """词项索引由哈希派生：同词同索引、落在 32 位无符号范围内。"""
    first = term_index("缓存")
    assert first == term_index("缓存")
    assert first != term_index("命中")
    assert 0 <= first <= 0xFFFFFFFF


def test_sparse_vector_rejects_mismatched_or_duplicate_indices():
    """稀疏向量是并行数组，长度不一致或索引重复都属契约错误。"""
    with pytest.raises(ValueError):
        SparseVector(indices=(1, 2), values=(0.1,))
    with pytest.raises(ValueError):
        SparseVector(indices=(1, 1), values=(0.1, 0.2))


def test_idf_formula_is_positive_and_decreases_with_frequency():
    """idf 恒为正；出现得越频繁权重越低。"""
    idf = build_idf([["a", "b"], ["a", "c"], ["a", "b"]])

    assert idf["a"] < idf["c"]
    assert all(value > 0 for value in idf.values())


def test_idf_of_empty_corpus_is_empty():
    """没有语料时不应抛出，返回空表由调用方决定默认权重。"""
    assert build_idf([]) == {}


def test_document_encoding_weights_rare_terms_higher():
    """文档侧编码：稀有词项的权重应大于高频词项。"""
    encoder = Bm25Encoder.fit(["缓存 缓存 缓存 命中率", "缓存 压测 Qdrant", "缓存 面试 记录"])
    sparse = encoder.encode_document("缓存 Qdrant")
    weights = dict(zip(sparse.indices, sparse.values, strict=True))

    assert weights[term_index("qdrant")] > weights[term_index("缓存")]


def test_empty_text_encodes_to_empty_sparse_vector():
    """空文本产生空稀疏向量，调用方据此跳过写入。"""
    encoder = Bm25Encoder.fit(["任意语料"])

    assert len(encoder.encode_document("")) == 0
    assert len(encoder.encode_query("   ")) == 0


def test_query_encoding_skips_length_normalisation():
    """查询侧不做长度归一化：长短问题不该因为长度不同而整体缩放权重。"""
    encoder = Bm25Encoder.fit(["缓存 命中率 压测 吞吐 检索 延迟"], b=0.75)

    short = encoder.encode_query("缓存")
    long = encoder.encode_query("缓存")

    assert short.values == long.values
    assert short.indices == long.indices


def test_unknown_terms_fall_back_to_default_idf():
    """未进 idf 表的词项退化为默认权重，而不是被丢弃。"""
    encoder = Bm25Encoder(idf={"已知": 2.0}, avg_doc_len=4.0, default_idf=1.0)

    sparse = encoder.encode_query("未知")

    assert len(sparse) == 1
    assert sparse.values[0] == pytest.approx(1.0)


def test_encoder_fit_uses_observed_average_length():
    """平均长度来自训练语料，影响文档侧的长度归一化。"""
    encoder = Bm25Encoder.fit(["一 二 三 四", "一 二"])

    assert encoder.avg_doc_len == pytest.approx(3.0)
    assert encoder.idf["一"] < encoder.idf["三"]


def test_weights_are_finite_for_repeated_terms():
    """重复词项经饱和函数后权重有限，不会因为 tf 很大而爆炸。"""
    encoder = Bm25Encoder(idf={"缓存": 1.5}, avg_doc_len=10.0)
    sparse = encoder.encode_query("缓存 " * 200)

    assert math.isfinite(sparse.values[0])
    assert sparse.values[0] > 1.5
