"""证据充分性（支持级别）评测指标（成员 C）。

M2 的三档评测（``eval_metrics``）回答的是"该找的片段找到没有"。这个模块回答另一个
问题：**一个看起来相关的片段，到底有没有把这条要求撑住**。两者不能互相替代——
Recall@3 已经饱和到 100%，却没有任何数字能说明"把部分支持判成完整支持"的比率。

口径（与 dev/13 的检索评测区分，见 dev/18）：

* 标注轴 ``axis`` 表达**期望的证据强度**，不是检索类别：
  ``fully_supported`` / ``partially_supported`` / ``no_evidence`` / ``old_version`` /
  ``deleted``；
* 判定状态沿用生产契约 ``supported`` / ``partial`` / ``not_evidenced``
  （``app.schemas.analysis.Match``、``app.agent.models.Assessment``），本模块不自造
  第四种状态；
* **错误支持率** ``false_support_rate`` 是本模块的主指标：在"不该被完整支持"的题目里，
  被判成 ``supported`` 的比例。它下降才算支持判定变好，Recall 上升不算；
* 未判定的结果（judge 未运行、外部服务未配置）单独计数，**不参与任何比率**，也不
  折算成 0 或 1。

证据标注用**原文锚点**而不是 chunk_id：切分参数一变 chunk_id 就漂，写死 UUID 的标注
会静默指向错误片段。锚点必须在指定版本里恰好命中一个 chunk，否则解析直接失败。
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import Field, model_validator

from app.rag.eval_metrics import percentile
from app.schemas.analysis import DocumentVersion
from app.schemas.common import ContractModel

SupportStatus = Literal["supported", "partial", "not_evidenced"]

# 判定状态：与生产契约同序，报告里按这个顺序渲染混淆矩阵。
SUPPORT_STATUSES: tuple[str, ...] = ("supported", "partial", "not_evidenced")

# 标注轴：期望的证据强度，不是检索类别。
SUPPORT_AXES: tuple[str, ...] = (
    "fully_supported",
    "partially_supported",
    "no_evidence",
    "old_version",
    "deleted",
)

# 口径版本：指标定义或标注字段一改就换版本号，旧结果不得被当成同一条件复述。
# v2：新增数字锚点覆盖率（引用里能否核对到要求中的数字）。
SUPPORT_METRIC_VERSION = "support-eval-v2"


class EvidenceAnchor(ContractModel):
    """用原文锚点标注证据：``anchor`` 必须是该版本文本里的连续子串。"""

    document_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    anchor: str = Field(min_length=4)


class SupportItem(ContractModel):
    """一条支持级别标注：要求原文 + 钉住的版本 + 期望判定与证据。"""

    id: str = Field(min_length=1)
    axis: Literal[
        "fully_supported",
        "partially_supported",
        "no_evidence",
        "old_version",
        "deleted",
    ]
    user_id: str = Field(min_length=1)
    requirement: str = Field(min_length=1)
    document_versions: list[DocumentVersion] = Field(min_length=1)
    expected_status: SupportStatus
    expected_evidence: list[EvidenceAnchor] = Field(default_factory=list)
    # 被取代的旧版本片段、已删除文档的片段：允许出现在上下文里，但绝不能作为引用。
    forbidden_evidence: list[EvidenceAnchor] = Field(default_factory=list)
    note: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_expectation(self):
        if len({(v.document_id, v.version) for v in self.document_versions}) != len(
            self.document_versions
        ):
            raise ValueError("同一文档只能有一个钉住的版本")
        if self.expected_status == "not_evidenced":
            if self.expected_evidence:
                raise ValueError("not_evidenced 不应标注期望证据")
        elif not self.expected_evidence:
            raise ValueError("supported/partial 必须标注期望证据")
        return self


class SupportJudgement(ContractModel):
    """判定输出；语义约束与 ``app.agent.models.Assessment`` 保持一致。

    刻意不复用 Assessment：那边带着 requirement_id 与 evidence_ids 的服务端语义，
    评测只需要"状态 + 引用了哪几块 + 缺什么"。但三态约束必须一致，否则评测口径
    与生产口径会分叉。
    """

    status: SupportStatus
    cited_chunk_ids: list[str] = Field(default_factory=list, max_length=10)
    missing_information: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_semantics(self):
        if len(self.cited_chunk_ids) != len(set(self.cited_chunk_ids)):
            raise ValueError("引用不得重复")
        if any(not item.strip() or len(item) > 1000 for item in self.missing_information):
            raise ValueError("缺失说明必须非空且有界")
        if self.status == "not_evidenced" and self.cited_chunk_ids:
            raise ValueError("not_evidenced 不得引用证据")
        if self.status in {"supported", "partial"} and not self.cited_chunk_ids:
            raise ValueError("supported/partial 必须引用证据")
        if self.status == "supported" and self.missing_information:
            raise ValueError("supported 不应有未解决的缺失说明")
        if self.status == "partial" and not self.missing_information:
            raise ValueError("partial 必须说明缺什么")
        return self


def resolve_anchors(
    anchors: Sequence[EvidenceAnchor],
    chunks_by_document: Mapping[tuple[str, int], Mapping[str, str]],
) -> tuple[str, ...]:
    """把锚点解析成 chunk_id。

    ``chunks_by_document`` 的键是 ``(document_id, version)``，值是 ``chunk_id -> 原文``。
    命中 0 个说明语料或切分变了、标注已失效；命中多个说明锚点不够具体、无法指向唯一
    证据。两种都必须显式失败，不能让标注悄悄指向别处。
    """
    resolved: list[str] = []
    for anchor in anchors:
        scope = chunks_by_document.get((anchor.document_id, anchor.version))
        if scope is None:
            raise ValueError(
                f"锚点 {anchor.document_id} v{anchor.version} 不在语料里：{anchor.anchor!r}"
            )
        hits = [chunk_id for chunk_id, text in scope.items() if anchor.anchor in text]
        if not hits:
            raise ValueError(
                f"锚点未命中任何 chunk（{anchor.document_id} v{anchor.version}）："
                f"{anchor.anchor!r}；语料或切分可能已变化，需重新校对标注"
            )
        if len(hits) > 1:
            raise ValueError(
                f"锚点在 {anchor.document_id} v{anchor.version} 命中 {len(hits)} 个 chunk，"
                f"标注不够具体：{anchor.anchor!r}"
            )
        resolved.append(hits[0])
    if len(resolved) != len(set(resolved)):
        raise ValueError("同一 chunk 被重复标注，无法区分证据来源")
    return tuple(resolved)


@dataclass(frozen=True)
class AssembledContext:
    """一次装配的上下文，以及**装不下的块**；不静默截断是硬要求。"""

    text: str
    block_ids: tuple[str, ...]
    dropped_ids: tuple[str, ...]

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def truncated(self) -> bool:
        return bool(self.dropped_ids)


def assemble_context(blocks: Sequence[tuple[str, str]], *, max_chars: int) -> AssembledContext:
    """按给定顺序装配上下文，块标签用 chunk_id（生产 prompt 就是按 chunk_id 引用）。

    顺序由调用方决定并保持稳定（按文档与序号），不在这里排序——不同 arm 的差异必须
    只来自"取哪些片段"，不能来自随机的块顺序。
    """
    if max_chars < 1:
        raise ValueError("max_chars 必须为正")
    parts: list[str] = []
    kept: list[str] = []
    dropped: list[str] = []
    used = 0
    for chunk_id, text in blocks:
        if used + len(text) > max_chars:
            dropped.append(chunk_id)
            continue
        parts.append(f"[{chunk_id}]\n{text}")
        kept.append(chunk_id)
        used += len(text)
    return AssembledContext(
        text="\n\n".join(parts), block_ids=tuple(kept), dropped_ids=tuple(dropped)
    )


@dataclass(frozen=True)
class SupportResult:
    """一条题目的评测结果；``judged_status`` 为 ``None`` 表示未判定。

    耗时按阶段拆开，口径与 dev/13 的检索评测一致：``embedding_ms`` 是向量化，
    ``store_ms`` 是向量库检索，``retrieval_ms`` 是整段检索（含重排与融合），
    ``judge_ms`` 是判定模型调用。``retrieval_ms - embedding_ms - store_ms`` 的残差
    就是重排等未单独计时的检索开销；全资料臂不检索，三段检索耗时都为 0。
    """

    item_id: str
    axis: str
    expected_status: str
    judged_status: str | None
    cited: tuple[str, ...]
    expected_evidence: tuple[str, ...]
    forbidden: tuple[str, ...]
    context_chars: int
    context_blocks: int
    dropped_blocks: int
    total_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    retrieval_ms: float = 0.0
    embedding_ms: float = 0.0
    store_ms: float = 0.0
    judge_ms: float = 0.0
    # 检索配置版本：空字符串表示该臂不检索（全资料基线）。
    retrieval_config_version: str = ""
    # 要求原文：数字锚点核对与人工复核都要用，落盘后不必回查标注文件。
    requirement: str = ""


def _decided(results: Sequence[SupportResult]) -> list[SupportResult]:
    return [result for result in results if result.judged_status is not None]


def _rate(numerator: int, denominator: int) -> float | None:
    """没有样本时返回 None，不返回 0——0 会被当成"没有这个错误"。"""
    return numerator / denominator if denominator else None


def status_accuracy(results: Sequence[SupportResult]) -> float | None:
    """判定状态与标注完全一致的比例；三态全对才算对。"""
    decided = _decided(results)
    return _rate(sum(r.judged_status == r.expected_status for r in decided), len(decided))


def false_support_rate(results: Sequence[SupportResult]) -> float | None:
    """错误支持率：不该被完整支持的题目里被判成 ``supported`` 的比例。

    这是本模块的主指标。分母只数"期望不是 supported"的题目——部分支持与无证据被
    判成完整支持，性质一样：用户会拿到一个没有依据的结论。
    """
    risky = [r for r in _decided(results) if r.expected_status != "supported"]
    return _rate(sum(r.judged_status == "supported" for r in risky), len(risky))


def over_refusal_rate(results: Sequence[SupportResult]) -> float | None:
    """过度保守率：本来撑得住的题目被判成 ``not_evidenced`` 的比例。

    与错误支持率是一对：只顾压低错误支持，很容易把所有东西都判成"没有证据"，
    那同样是把有依据的经历否掉。
    """
    supported = [r for r in _decided(results) if r.expected_status == "supported"]
    return _rate(sum(r.judged_status == "not_evidenced" for r in supported), len(supported))


def stale_citation_rate(results: Sequence[SupportResult]) -> float | None:
    """引用失效证据的比例：引用了旧版本或被删除文档片段的比例。

    这类引用比"没引用"更糟——它把已经被取代的结论当成现行依据。
    """
    guarded = [r for r in _decided(results) if r.forbidden]
    stale = sum(bool(set(r.cited) & set(r.forbidden)) for r in guarded)
    return _rate(stale, len(guarded))


def citation_precision(results: Sequence[SupportResult]) -> float | None:
    """引用正确率：被判成 supported/partial 的题目里，引用落在期望证据上的比例。"""
    citing = [
        r for r in _decided(results) if r.judged_status in {"supported", "partial"} and r.cited
    ]
    if not citing:
        return None
    hits = sum(len(set(r.cited) & set(r.expected_evidence)) for r in citing)
    return hits / sum(len(r.cited) for r in citing)


def citation_recall(results: Sequence[SupportResult]) -> float | None:
    """引用召回：期望证据中被实际引用的比例；缺引用与错引用是两种不同失败。"""
    citing = [
        r
        for r in _decided(results)
        if r.judged_status in {"supported", "partial"} and r.expected_evidence
    ]
    if not citing:
        return None
    hits = sum(len(set(r.cited) & set(r.expected_evidence)) for r in citing)
    return hits / sum(len(r.expected_evidence) for r in citing)


def confusion_matrix(results: Sequence[SupportResult]) -> dict[str, dict[str, int]]:
    """期望状态 × 判定状态的计数；三态之间的每一次挪动都能被看到。"""
    matrix = {expected: dict.fromkeys(SUPPORT_STATUSES, 0) for expected in SUPPORT_STATUSES}
    for result in _decided(results):
        matrix[result.expected_status][result.judged_status] += 1
    return matrix


# 数字与版本号锚点：只要原文里出现过同样的数，就说明模型有机会核对它。
_NUMERIC_ANCHOR = re.compile(r"\d+(?:\.\d+)*")


def numeric_anchors(text: str) -> tuple[str, ...]:
    """取出文本里的数字/版本号锚点（去重、保序）。

    只抓数字是因为它可机械核对：要求写了"每秒约 300 次请求"，被引用的片段里到底有没有
    "300"，不需要模型判断。词语级核对需要同义词表，不属于这一层的职责。
    """
    seen: list[str] = []
    for match in _NUMERIC_ANCHOR.finditer(text):
        value = match.group(0)
        if value not in seen:
            seen.append(value)
    return tuple(seen)


def _contains_anchor(text: str, anchor: str) -> bool:
    """按"独立数字"匹配：``1300`` 不算包含 ``300``，``16.25`` 不算包含 ``16.2``。"""
    pattern = re.compile(rf"(?<![\d.]){re.escape(anchor)}(?![\d.])")
    return pattern.search(text) is not None


def uncovered_numeric_anchors(requirement: str, cited_texts: Sequence[str]) -> tuple[str, ...]:
    """要求里的数字锚点中，被引用片段**一处都没有**覆盖的那些。

    返回空表示"所有数字都能在引用里核对到"；返回非空就是在提醒：模型给了结论，
    但它引用的原文里根本没有这个数。
    """
    texts = list(cited_texts)
    return tuple(
        anchor
        for anchor in numeric_anchors(requirement)
        if not any(_contains_anchor(text, anchor) for text in texts)
    )


def numeric_anchor_failures(
    results: Sequence[SupportResult], chunk_texts: Mapping[str, str]
) -> list[dict]:
    """列出"判了 supported/partial，但引用里核对不到要求中数字"的题目。"""
    failures = []
    for result in _decided(results):
        if result.judged_status not in {"supported", "partial"} or not result.cited:
            continue
        missing = uncovered_numeric_anchors(
            result.requirement,
            [chunk_texts[chunk_id] for chunk_id in result.cited if chunk_id in chunk_texts],
        )
        if missing:
            failures.append({"item_id": result.item_id, "missing": list(missing)})
    return failures


def numeric_anchor_rate(
    results: Sequence[SupportResult], chunk_texts: Mapping[str, str]
) -> float | None:
    """数字锚点覆盖率：要求里写了数字的题里，引用片段能核对到这些数字的比例。

    它补的是"引用召回"管不到的一段：召回只问"有没有引用标注的那一块"，不问
    "被引用的原文里到底有没有被核对的数"。分母只数"要求含数字、且判成 supported/partial、
    且有引用"的题——其余题目没有可核对的锚点，不能算成通过。
    """
    checked = 0
    covered = 0
    for result in _decided(results):
        if result.judged_status not in {"supported", "partial"} or not result.cited:
            continue
        anchors = numeric_anchors(result.requirement)
        if not anchors:
            continue
        checked += 1
        cited_texts = [
            chunk_texts[chunk_id] for chunk_id in result.cited if chunk_id in chunk_texts
        ]
        if not uncovered_numeric_anchors(result.requirement, cited_texts):
            covered += 1
    return _rate(covered, checked)


def summarize(
    results: Sequence[SupportResult],
    *,
    arm: str,
    chunk_texts: Mapping[str, str] | None = None,
) -> dict:
    """汇总成可落盘的字典；未判定、上下文体积与成本都单列，不混进比率。

    ``chunk_texts`` 给出时才算数字锚点覆盖率——那是一项机械核对，需要被引用片段的原文；
    不给就标注为未判定，而不是当作通过。
    """
    decided = _decided(results)
    by_axis: dict[str, dict[str, int]] = {}
    for axis in SUPPORT_AXES:
        group = [r for r in results if r.axis == axis]
        if not group:
            continue
        by_axis[axis] = {
            "count": len(group),
            "decided": sum(r.judged_status is not None for r in group),
            "false_support": sum(
                r.judged_status == "supported" and r.expected_status != "supported" for r in group
            ),
        }
    chars = [r.context_chars for r in results]
    latencies = [r.total_ms for r in decided]
    return {
        "arm": arm,
        "metric_version": SUPPORT_METRIC_VERSION,
        "counts": {
            "total": len(results),
            "decided": len(decided),
            "undecided": len(results) - len(decided),
            "by_axis": by_axis,
        },
        # 本臂实际用到的检索配置版本：臂配错策略时这里会露出来。
        "config_versions": sorted(
            {r.retrieval_config_version for r in results if r.retrieval_config_version}
        ),
        "status_accuracy": status_accuracy(results),
        "false_support_rate": false_support_rate(results),
        "over_refusal_rate": over_refusal_rate(results),
        "stale_citation_rate": stale_citation_rate(results),
        "citation_precision": citation_precision(results),
        "citation_recall": citation_recall(results),
        "numeric_anchor_rate": (
            numeric_anchor_rate(results, chunk_texts) if chunk_texts is not None else None
        ),
        "numeric_anchor": _numeric_anchor_block(results, chunk_texts),
        "confusion": confusion_matrix(results),
        "context": {
            "mean_chars": round(sum(chars) / len(chars), 1) if chars else 0.0,
            "max_chars": max(chars) if chars else 0,
            "p95_chars": percentile([float(value) for value in chars], 0.95),
            "mean_blocks": round(sum(r.context_blocks for r in results) / len(results), 1)
            if results
            else 0.0,
            "truncated_items": sum(r.dropped_blocks > 0 for r in results),
        },
        "cost": {
            "input_tokens": sum(r.input_tokens for r in decided),
            "output_tokens": sum(r.output_tokens for r in decided),
        },
        "latency_ms": {
            "count": len(latencies),
            "p50": round(percentile(latencies, 0.50), 1),
            "p95": round(percentile(latencies, 0.95), 1),
            **_latency_stages(decided),
        },
    }


def _mean_or_none(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _numeric_anchor_block(
    results: Sequence[SupportResult], chunk_texts: Mapping[str, str] | None
) -> dict:
    """数字锚点的可判定条数与失败清单；未提供原文时明确写"未判定"。"""
    if chunk_texts is None:
        return {"applicable": 0, "covered": 0, "failures": [], "note": "未提供语料原文，未判定"}
    applicable = 0
    covered = 0
    for result in _decided(results):
        if result.judged_status not in {"supported", "partial"} or not result.cited:
            continue
        if not numeric_anchors(result.requirement):
            continue
        applicable += 1
        cited_texts = [
            chunk_texts[chunk_id] for chunk_id in result.cited if chunk_id in chunk_texts
        ]
        if not uncovered_numeric_anchors(result.requirement, cited_texts):
            covered += 1
    return {
        "applicable": applicable,
        "covered": covered,
        "failures": numeric_anchor_failures(results, chunk_texts),
    }


def _latency_stages(decided: Sequence[SupportResult]) -> dict:
    """分阶段耗时：embedding / 向量库检索 / 重排残差 / 判定模型。

    全资料臂不检索，它的检索三段恒为 0 而不是缺失——报告里一眼能看出"这条臂没有检索
    开销"，不会被误读成"检索零成本"。
    """
    embedding = [r.embedding_ms for r in decided]
    store = [r.store_ms for r in decided]
    retrieval = [r.retrieval_ms for r in decided]
    judge = [r.judge_ms for r in decided]
    # 重排等未单独计时的检索开销：用整段检索减去已计时的两段，负数按 0 处理。
    rerank = [max(0.0, r.retrieval_ms - r.embedding_ms - r.store_ms) for r in decided]
    return {
        "mean_embedding": _mean_or_none(embedding),
        "mean_store": _mean_or_none(store),
        "mean_rerank": _mean_or_none(rerank),
        "mean_judge": _mean_or_none(judge),
        "retrieval_p50": round(percentile(retrieval, 0.50), 1) if retrieval else None,
        "retrieval_p95": round(percentile(retrieval, 0.95), 1) if retrieval else None,
    }


def resolution_note(decided: int) -> str:
    """样本分辨率：样本这么少时必须写出"1 条等于多少"，否则费率会被读成精度。"""
    if decided < 1:
        return "无可判定样本，本轮不产出任何比率"
    if decided == 1:
        return "仅 1 条可判定样本：1 条 = 1.0000，比率不具区分度"
    return f"可判定样本 {decided} 条：1 条 = {1 / decided:.4f}，比率只有量级意义"


_METRIC_LABELS = (
    ("status_accuracy", "状态完全一致率"),
    ("false_support_rate", "错误支持率 ↓"),
    ("over_refusal_rate", "过度保守率 ↓"),
    ("stale_citation_rate", "引用失效证据率 ↓"),
    ("citation_precision", "引用正确率 ↑"),
    ("citation_recall", "引用召回 ↑"),
    ("numeric_anchor_rate", "数字锚点覆盖率 ↑"),
)


def _fmt(value: float | None) -> str:
    return "无样本" if value is None else f"{value:.4f}"


def _fmt_ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}"


def _fmt_numeric_anchor(summary: dict) -> str:
    """数字锚点覆盖率的三态显示：未判定 / 无可核对 / 比率。

    它与其它比率不同——"没有可核对的数字"不是"没有数据"，也不该显示成 0。
    """
    block = summary.get("numeric_anchor") or {}
    if block.get("note"):
        return "未判定"
    if not block.get("applicable"):
        return "无可核对"
    value = summary.get("numeric_anchor_rate")
    return "无样本" if value is None else f"{value:.4f}"


def _config_lines(summary: dict) -> list[str]:
    """把本臂实际用到的检索配置版本写进报告；全资料臂写明"不检索"。"""
    versions = summary.get("config_versions") or []
    if not versions:
        return ["- 检索配置：不检索（全资料基线）"]
    return [f"- 检索配置：`{version}`" for version in versions]


def _latency_line(latency: dict) -> str:
    """把分阶段耗时写成一行；检索三段全 0 时标注"不检索"，不装成零开销。"""
    if not latency.get("retrieval_p50"):
        return f"分阶段耗时：不检索（仅判定 {_fmt_ms(latency.get('mean_judge'))} ms）"
    return (
        f"分阶段耗时（均值，ms）：embedding {_fmt_ms(latency.get('mean_embedding'))} / "
        f"向量库 {_fmt_ms(latency.get('mean_store'))} / "
        f"重排等残差 {_fmt_ms(latency.get('mean_rerank'))} / "
        f"判定 {_fmt_ms(latency.get('mean_judge'))}；"
        f"检索段 P50 {_fmt_ms(latency.get('retrieval_p50'))} / "
        f"P95 {_fmt_ms(latency.get('retrieval_p95'))}"
    )


def render_markdown(
    summary: dict,
    *,
    judge_version: str,
    notes: Sequence[str] = (),
) -> str:
    """把单个 arm 的汇总渲染成报告；未判定与口径说明必须出现在数字旁边。"""
    counts = summary["counts"]
    lines = [
        f"### 对照臂 `{summary['arm']}`",
        "",
        f"- 指标口径：`{summary['metric_version']}`；判定模型：`{judge_version}`",
        f"- 题目 {counts['total']} 条，已判定 {counts['decided']} 条，"
        f"未判定 {counts['undecided']} 条",
        *_config_lines(summary),
        f"- {resolution_note(counts['decided'])}",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
    ]
    for key, label in _METRIC_LABELS:
        lines.append(f"| {label} (`{key}`) | {_fmt(summary[key])} |")

    numeric = summary.get("numeric_anchor") or {}
    if numeric.get("note"):
        lines += ["", f"> 数字锚点覆盖率未判定：{numeric['note']}"]
    elif not numeric.get("applicable"):
        lines += ["", "> 数字锚点覆盖率无可核对锚点：本题集的要求里没有数字"]
    if numeric.get("failures"):
        lines += ["", "数字核对失败（判了有支持，但引用里找不到要求中的数字）："]
        for failure in numeric["failures"]:
            lines.append(f"- `{failure['item_id']}`：缺 {', '.join(failure['missing'])}")
    context = summary["context"]
    latency = summary["latency_ms"]
    lines += [
        "",
        f"上下文体积：平均 {context['mean_chars']:.0f} 字 / 最多 {context['max_chars']} 字 / "
        f"平均 {context['mean_blocks']:.1f} 块；超预算被跳过的题 {context['truncated_items']} 条",
        f"成本：输入 {summary['cost']['input_tokens']} tokens，"
        f"输出 {summary['cost']['output_tokens']} tokens；"
        f"总耗时 P50 {latency['p50']:.0f} ms / P95 {latency['p95']:.0f} ms",
        _latency_line(latency),
        "",
        "| 期望 \\ 判定 | supported | partial | not_evidenced |",
        "| --- | --- | --- | --- |",
    ]
    for expected, row in summary["confusion"].items():
        lines.append(
            f"| {expected} | {row['supported']} | {row['partial']} | {row['not_evidenced']} |"
        )
    for note in notes:
        lines += ["", f"> {note}"]
    return "\n".join(lines)


def render_comparison(summaries: Sequence[dict], *, notes: Sequence[str] = ()) -> str:
    """把多个 arm 并排成一张表——"全资料对照"只有并排才有意义。"""
    if not summaries:
        raise ValueError("至少需要一个 arm 的汇总")
    header = "| 指标 | " + " | ".join(f"`{item['arm']}`" for item in summaries) + " |"
    lines = [
        "## 对照臂并排",
        "",
        header,
        "| --- |" + " --- |" * len(summaries),
    ]
    for key, label in _METRIC_LABELS:
        if key == "numeric_anchor_rate":
            cells = " | ".join(_fmt_numeric_anchor(item) for item in summaries)
        else:
            cells = " | ".join(_fmt(item[key]) for item in summaries)
        lines.append(f"| {label} | {cells} |")
    cells = " | ".join(f"{item['context']['mean_chars']:g}" for item in summaries)
    lines.append(f"| 平均上下文（字） | {cells} |")
    cells = " | ".join(f"{item['context']['mean_blocks']:g}" for item in summaries)
    lines.append(f"| 平均上下文（块） | {cells} |")
    # 耗时按阶段并排：全资料臂不检索，这几行会显示"—"，一眼能看出不可跨臂比较。
    for key, label in (
        ("mean_embedding", "平均 embedding（ms）"),
        ("mean_store", "平均向量库检索（ms）"),
        ("mean_rerank", "平均重排残差（ms）"),
        ("mean_judge", "平均判定（ms）"),
    ):
        cells = " | ".join(
            "—"
            if not item["latency_ms"].get("retrieval_p50") and key != "mean_judge"
            else _fmt_ms(item["latency_ms"].get(key))
            for item in summaries
        )
        lines.append(f"| {label} | {cells} |")
    for note in notes:
        lines += ["", f"> {note}"]
    return "\n".join(lines)
