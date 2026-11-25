"""支持级评测运行器的关键保证：已删除文档不得进入上下文。

运行器是脚本而非包模块，这里按路径导入；脚本导入期只定义常量，``backend()`` 是延迟
导入，因此可以在单测里直接引用而不需要任何外部服务。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import rag_support_eval as runner  # noqa: E402

INDEX = {
    ("doc_a", 1): {"a1": "甲段", "a2": "甲段二"},
    ("doc_b", 1): {"b1": "乙段"},
}


def item(
    axis: str,
    *,
    pinned: tuple[tuple[str, int], ...] = (("doc_a", 1), ("doc_b", 1)),
    forbidden: tuple[tuple[str, int], ...] = (),
) -> SimpleNamespace:
    """最小化的标注替身：运行器只用到这几个字段。"""
    return SimpleNamespace(
        id="i",
        axis=axis,
        document_versions=[
            SimpleNamespace(document_id=document_id, version=version)
            for document_id, version in pinned
        ],
        forbidden_evidence=[
            SimpleNamespace(document_id=document_id, version=version, anchor="锚点文字")
            for document_id, version in forbidden
        ],
    )


def test_deleted_documents_comes_from_the_deleted_axis_only():
    """只有删除轴标注的文档算被删除；其他轴不允许悄悄删掉语料。"""
    items = [item("fully_supported"), item("deleted", forbidden=(("doc_b", 1),))]

    assert runner.deleted_documents(items) == {"doc_b"}


def test_deleted_document_leaves_context_while_others_keep_full_corpus():
    """删除轴逐题生效：那一道题看不到已删除片段，其他题仍是完整语料。"""
    kept = runner.pinned_chunk_blocks(item("fully_supported"), INDEX, excluded_documents=set())
    excluded = runner.pinned_chunk_blocks(item("deleted"), INDEX, excluded_documents={"doc_b"})

    assert [chunk_id for chunk_id, _ in kept] == ["a1", "a2", "b1"]
    assert [chunk_id for chunk_id, _ in excluded] == ["a1", "a2"]


def test_pinned_chunk_order_is_stable():
    """上下文顺序必须由钉住的版本决定，不能随 arm 变化——否则差异无法归因。"""
    blocks = runner.pinned_chunk_blocks(item("fully_supported"), INDEX, excluded_documents=set())

    assert blocks == [("a1", "甲段"), ("a2", "甲段二"), ("b1", "乙段")]


def test_missing_pinned_version_fails_loudly():
    """钉住的版本不在语料里就显式失败，不能拿残缺上下文继续算比率。"""
    unsynced = item("fully_supported", pinned=(("doc_z", 1),))

    with pytest.raises(SystemExit, match="不在语料里"):
        runner.pinned_chunk_blocks(unsynced, INDEX, excluded_documents=set())


def test_provider_judge_is_selected_but_never_silently_replaced():
    """--judge provider 要么真的建出判定器，要么显式失败；没有离线替身分支。"""
    assert isinstance(runner.build_judge("none"), runner.NullJudge)
    with pytest.raises(SystemExit, match="未知判定模式"):
        runner.build_judge("fake")
