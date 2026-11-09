"""M2 混合检索融合权重的开发集调参（成员 C，issue #25）。

规则（dev/13 第 4 节）：参数只能在**开发集**上定，评测集只用来报告一次。本脚本因此
强制只跑 ``dev`` 切分，并在结果里写清楚样本量——12 条可回答问题意味着一条题就是
0.083 的 Recall@1 变化，差异小于这个量级的权重不该被当作"更优"。

用法（需要真实 Embedding 与 Qdrant，索引必须已存在）：

    python scripts/m2_rag_tune.py --weights 0.3 0.5 0.7 --no-proxy

产物：``results/tuning.json`` 记录每个权重的开发集指标，供选择默认权重留痕。
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import m2_rag_eval as runner  # noqa: E402

DEV_SPLIT = "dev"
# 可回答问题数量：用于向读者交代"一条题值多少指标"。
SINGLE_QUESTION_EFFECT = {"recall_at_1": 1 / 12, "mrr": 1 / 12}


def dev_questions() -> list[dict]:
    """取开发集问题；评测集的问题绝不出现在调参里。"""
    return [q for q in runner.load_questions() if q["split"] == DEV_SPLIT]


def report_row(weight: float, summary: dict) -> dict:
    """把一次权重的开发集结果压成一行，便于横向比较。"""
    answerable = summary["answerable"]
    return {
        "sparse_weight": weight,
        "count": answerable["count"],
        "recall_at_1": answerable.get("recall_at_1"),
        "recall_at_3": answerable.get("recall_at_3"),
        "mrr": answerable.get("mrr"),
        "violations": {kind: entry["violations"] for kind, entry in summary["negatives"].items()},
    }


def render(rows: list[dict]) -> str:
    """把各权重结果排成表；不做"最优"标记，选择由人根据噪声水平决定。"""
    lines = [
        f"开发集权重扫描（{DEV_SPLIT} 切分，可回答问题 {rows[0]['count']} 条）",
        "",
        "| sparse_weight | Recall@1 | Recall@3 | MRR | 负例违规 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        violations = ", ".join(f"{kind}:{count}" for kind, count in row["violations"].items())
        lines.append(
            f"| {row['sparse_weight']:.2f} | {row['recall_at_1']:.4f} | "
            f"{row['recall_at_3']:.4f} | {row['mrr']:.4f} | {violations} |"
        )
    lines += [
        "",
        f"注意：可回答问题仅 {rows[0]['count']} 条，一条题即影响 "
        f"Recall@1 {SINGLE_QUESTION_EFFECT['recall_at_1']:.3f}；"
        "小于该量级的差异不足以支持「更优」结论。",
    ]
    return "\n".join(lines)


async def sweep(weights: list[float], *, top_k: int | None, no_proxy: bool) -> list[dict]:
    """在开发集上按给定权重逐个评测混合检索。"""
    settings = runner.eval_settings(no_proxy=no_proxy)
    manifest = runner.load_manifest()
    if manifest["chunk_max_chars"] != settings.chunk_max_chars:
        raise SystemExit("切分参数与语料清单不一致，拒绝调参（chunk_id 会漂移）")
    chunk_texts = runner.corpus_chunk_map(manifest, settings)
    encoder = runner.build_sparse_encoder(manifest, settings, chunk_texts)
    questions = dev_questions()
    print(f"开发集问题 {len(questions)} 条，开始扫描 {len(weights)} 个权重")

    rows: list[dict] = []
    for weight in weights:
        print(f"\n=== sparse_weight = {weight:.2f} ===")
        strategy_settings = settings.model_copy(
            update={"retrieval_strategy": "hybrid", "hybrid_sparse_weight": weight}
        )
        suffix = f".{DEV_SPLIT}.w{weight:.2f}"
        results = await runner.evaluate(
            strategy_settings,
            questions,
            "hybrid",
            resume=True,
            top_k=top_k,
            sparse_encoder=encoder,
            chunk_texts=chunk_texts,
            suffix=suffix,
        )
        summary = runner.backend().eval_metrics.summarize(results)
        rows.append(report_row(weight, summary))
        print(
            f"  Recall@1 {summary['answerable']['recall_at_1']:.4f}  "
            f"MRR {summary['answerable']['mrr']:.4f}"
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="混合检索融合权重的开发集调参")
    parser.add_argument("--weights", nargs="+", type=float, default=[0.3, 0.5, 0.7])
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--no-proxy", action="store_true", help="绕过系统代理直连外部服务")
    args = parser.parse_args()
    for weight in args.weights:
        if not 0 < weight < 1:
            raise SystemExit(f"权重必须落在 (0, 1) 开区间：{weight}")

    rows = asyncio.run(sweep(args.weights, top_k=args.top_k, no_proxy=args.no_proxy))
    table = render(rows)
    runner.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (runner.RESULTS_DIR / "tuning.json").write_text(
        json.dumps(
            {
                "split": DEV_SPLIT,
                "single_question_effect": SINGLE_QUESTION_EFFECT,
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print("\n" + table)


if __name__ == "__main__":
    main()
