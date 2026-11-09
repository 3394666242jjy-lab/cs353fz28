"""M2 检索评测指标（成员 C，issue #25）。

三档策略必须在同一口径下比较，因此指标计算独立成模块并有单测，不允许在实验脚本里
各算一套。口径见 dev/13：

* Recall@k    —— 标注相关集合被前 k 个结果覆盖的比例；
* MRR         —— 首个相关结果的倒数排名；
* 引用正确性  —— 前 k 个结果中属于标注相关集合的比例；
* 误召回率    —— 负例里出现违规结果的占比，**单独统计**，不与 Recall 混算。

负例分两种期望，不能合并：``empty`` 要求检索结果为空；``forbidden_absent`` 允许有
其他结果，只禁止被取代的旧版本 chunk 出现。
"""

from collections.abc import Sequence
from dataclasses import dataclass

KS = (1, 3, 5)

# 负例类别；deleted 依赖物理清理链路（S1b），在清理落地前只记录不判定。
NEGATIVE_KINDS = ("no_evidence", "cross_user", "old_version", "deleted")
PENDING_KINDS = ("deleted",)


@dataclass(frozen=True)
class QueryResult:
    """一条问题的检索结果与耗时；耗时按阶段拆分，便于定位开销来源。"""

    question_id: str
    kind: str
    expect: str | None
    relevant: tuple[str, ...]
    forbidden: tuple[str, ...]
    returned: tuple[str, ...]
    total_ms: float
    embedding_ms: float
    search_ms: float

    @property
    def is_answerable(self) -> bool:
        """可回答问题才计入 Recall / MRR / 引用正确性。"""
        return self.kind == "answerable"

    def hits_at(self, k: int) -> tuple[str, ...]:
        """返回前 k 个结果的 chunk_id。"""
        return self.returned[:k]


def recall_at_k(result: QueryResult, k: int) -> float:
    """标注相关集合被前 k 个结果覆盖的比例；无标注时视为 0。"""
    if not result.relevant:
        return 0.0
    hit = set(result.hits_at(k)) & set(result.relevant)
    return len(hit) / len(result.relevant)


def reciprocal_rank(result: QueryResult) -> float:
    """首个相关结果的倒数排名；没有相关结果时为 0。"""
    relevant = set(result.relevant)
    for rank, chunk_id in enumerate(result.returned, start=1):
        if chunk_id in relevant:
            return 1.0 / rank
    return 0.0


def citation_precision_at_k(result: QueryResult, k: int) -> float:
    """前 k 个结果中属于标注相关集合的比例；没有任何结果时为 0。

    这个指标回答"引用得对不对"，与 Recall 回答"该找的找到没有"互补：只堆候选
    可以把 Recall 刷高，但引用正确性会掉下来。
    """
    hits = result.hits_at(k)
    if not hits:
        return 0.0
    return len(set(hits) & set(result.relevant)) / len(hits)


def is_violation(result: QueryResult) -> bool:
    """负例是否违规：``empty`` 看非空，``forbidden_absent`` 看是否出现禁用 chunk。"""
    if result.is_answerable:
        return False
    if result.expect == "forbidden_absent":
        return bool(set(result.returned) & set(result.forbidden))
    return bool(result.returned)


def percentile(values: Sequence[float], ratio: float) -> float:
    """最近秩百分位；空序列返回 0。样本很少时也不用插值，避免制造精度幻觉。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(ratio * (len(ordered) - 1))))
    return ordered[index]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize(results: Sequence[QueryResult], ks: Sequence[int] = KS) -> dict:
    """汇总成可直接写入结果文件的字典；负例按类别分开统计。"""
    answerable = [r for r in results if r.is_answerable]
    negatives = [r for r in results if not r.is_answerable]
    latencies = [r.total_ms for r in results]

    answerable_block: dict = {"count": len(answerable)}
    for k in ks:
        answerable_block[f"recall_at_{k}"] = _mean([recall_at_k(r, k) for r in answerable])
        answerable_block[f"citation_precision_at_{k}"] = _mean(
            [citation_precision_at_k(r, k) for r in answerable]
        )
    answerable_block["mrr"] = _mean([reciprocal_rank(r) for r in answerable])

    negative_block: dict = {}
    for kind in NEGATIVE_KINDS:
        group = [r for r in negatives if r.kind == kind]
        if not group:
            continue
        entry = {"count": len(group), "violations": sum(is_violation(r) for r in group)}
        entry["rate"] = entry["violations"] / entry["count"]
        if kind in PENDING_KINDS:
            # 清理链路未落地时无从构造"点已被物理删除"的场景，只记录不判定。
            entry["pending"] = True
        negative_block[kind] = entry

    return {
        "answerable": answerable_block,
        "negatives": negative_block,
        "latency_ms": {
            "count": len(latencies),
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "mean_embedding": _mean([r.embedding_ms for r in results]),
            "mean_search": _mean([r.search_ms for r in results]),
        },
    }


def render_markdown(summary: dict, *, strategy: str, config_version: str) -> str:
    """把汇总渲染成可贴进报告的表；未判定的负例明确标注，不混进结论。"""
    lines = [
        f"### 策略 `{strategy}`",
        "",
        f"- 检索配置版本：`{config_version}`",
        f"- 可回答问题：{summary['answerable']['count']}",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
    ]
    answerable = summary["answerable"]
    for key in ("recall_at_1", "recall_at_3", "recall_at_5", "mrr"):
        if key in answerable:
            lines.append(f"| {key} | {answerable[key]:.4f} |")
    for key in ("citation_precision_at_1", "citation_precision_at_3", "citation_precision_at_5"):
        if key in answerable:
            lines.append(f"| {key} | {answerable[key]:.4f} |")

    latency = summary["latency_ms"]
    lines += [
        "",
        f"耗时（ms）：P50 {latency['p50']:.1f}，P95 {latency['p95']:.1f}，"
        f"平均 embedding {latency['mean_embedding']:.1f} / 检索 {latency['mean_search']:.1f}",
        "",
        "| 负例类别 | 数量 | 违规 | 违规率 |",
        "| --- | --- | --- | --- |",
    ]
    for kind, entry in summary["negatives"].items():
        rate = "未判定（待清理链路）" if entry.get("pending") else f"{entry['rate']:.2%}"
        lines.append(f"| {kind} | {entry['count']} | {entry['violations']} | {rate} |")
    return "\n".join(lines)
