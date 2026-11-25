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
SUPPORT_METRIC_VERSION = "support-eval-v1"


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
    """一条题目的评测结果；``judged_status`` 为 ``None`` 表示未判定。"""

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


def summarize(results: Sequence[SupportResult], *, arm: str) -> dict:
    """汇总成可落盘的字典；未判定、上下文体积与成本都单列，不混进比率。"""
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
        "status_accuracy": status_accuracy(results),
        "false_support_rate": false_support_rate(results),
        "over_refusal_rate": over_refusal_rate(results),
        "stale_citation_rate": stale_citation_rate(results),
        "citation_precision": citation_precision(results),
        "citation_recall": citation_recall(results),
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
        },
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
)


def _fmt(value: float | None) -> str:
    return "无样本" if value is None else f"{value:.4f}"


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
        f"- {resolution_note(counts['decided'])}",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
    ]
    for key, label in _METRIC_LABELS:
        lines.append(f"| {label} (`{key}`) | {_fmt(summary[key])} |")

    context = summary["context"]
    lines += [
        "",
        f"上下文体积：平均 {context['mean_chars']:.0f} 字 / 最多 {context['max_chars']} 字 / "
        f"平均 {context['mean_blocks']:.1f} 块；超预算被跳过的题 {context['truncated_items']} 条",
        f"成本：输入 {summary['cost']['input_tokens']} tokens，"
        f"输出 {summary['cost']['output_tokens']} tokens；"
        f"耗时 P50 {summary['latency_ms']['p50']:.0f} ms / "
        f"P95 {summary['latency_ms']['p95']:.0f} ms",
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
        cells = " | ".join(_fmt(item[key]) for item in summaries)
        lines.append(f"| {label} | {cells} |")
    cells = " | ".join(f"{item['context']['mean_chars']:g}" for item in summaries)
    lines.append(f"| 平均上下文（字） | {cells} |")
    cells = " | ".join(f"{item['context']['mean_blocks']:g}" for item in summaries)
    lines.append(f"| 平均上下文（块） | {cells} |")
    for note in notes:
        lines += ["", f"> {note}"]
    return "\n".join(lines)
