"""M2 三档检索策略评测运行器（成员 C，issue #25）。

在**独立集合**（默认 ``careerpilot_eval``）上索引合成语料，按问题集逐条检索并计算
指标。不改动开发集合 ``careerpilot_chunks``，也不走 HTTP API——检索质量要在隔离
状态下测量，而检索本身发生在分析 worker 内部、没有独立端点。

口径与约束见 dev/13 与 ``backend/tests/data/rag_eval/README.md``：

* 三档策略用同一个 ``retrieval_config_version`` 记录条件，结果文件按版本落盘；
* 负例分 ``empty`` 与 ``forbidden_absent`` 两种期望，``deleted`` 类别在清理链路
  就绪前只记录不判定；
* 外部服务未配置时**必须显式失败**，不得用离线替身冒充真实结果。

用法（需要真实 Embedding 服务与 Qdrant）：

    python scripts/m2_rag_eval.py --rebuild                 # 重建集合+索引+评测 dense
    python scripts/m2_rag_eval.py --evaluate-only --resume  # 断点续跑
    python scripts/m2_rag_eval.py --dump-labels             # 打印标注对照表供人工复核

本机若装了系统代理而代理未运行，httpx 会读到注册表里的代理地址并连接失败；此时加
``--no-proxy`` 直连外部服务。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
DATA_DIR = BACKEND / "tests" / "data" / "rag_eval"
CORPUS_DIR = DATA_DIR / "corpus"
QUESTIONS_PATH = DATA_DIR / "questions.jsonl"
MANIFEST_PATH = DATA_DIR / "manifest.json"
RESULTS_DIR = DATA_DIR / "results"

# 评测用独立集合：语料会被反复重建，绝不能污染开发集合。
EVAL_COLLECTION = "careerpilot_eval"

_BACKEND = None


def backend() -> SimpleNamespace:
    """延迟导入后端模块：脚本位于仓库根，运行期才把 backend 加入 sys.path。"""
    global _BACKEND
    if _BACKEND is None:
        if str(BACKEND) not in sys.path:
            sys.path.insert(0, str(BACKEND))
        import app.rag.eval_metrics as eval_metrics
        from app.config import Settings, get_settings
        from app.rag.bm25 import Bm25Encoder
        from app.rag.chunking import chunk_document, parse_document
        from app.rag.embeddings import get_embedding_provider
        from app.rag.indexer import index_document, make_chunk_id
        from app.rag.retrieval import QdrantEvidenceSource
        from app.rag.vector_store import VectorStoreError, get_vector_store
        from app.schemas.analysis import DocumentVersion

        _BACKEND = SimpleNamespace(
            Settings=Settings,
            eval_metrics=eval_metrics,
            Bm25Encoder=Bm25Encoder,
            get_settings=get_settings,
            parse_document=parse_document,
            chunk_document=chunk_document,
            make_chunk_id=make_chunk_id,
            index_document=index_document,
            get_embedding_provider=get_embedding_provider,
            QdrantEvidenceSource=QdrantEvidenceSource,
            VectorStoreError=VectorStoreError,
            get_vector_store=get_vector_store,
            DocumentVersion=DocumentVersion,
        )
    return _BACKEND


class TimedEmbedder:
    """包装 Embedding Provider，累计本阶段耗时；接口原样透传。"""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.elapsed_ms = 0.0

    def reset(self) -> None:
        self.elapsed_ms = 0.0

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        start = time.perf_counter()
        vectors = await self._inner.embed_texts(texts)
        self.elapsed_ms += (time.perf_counter() - start) * 1000
        return vectors

    def __getattr__(self, name):
        return getattr(self._inner, name)


class TimedStore:
    """包装 VectorStore，累计检索耗时；接口原样透传。"""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.elapsed_ms = 0.0

    def reset(self) -> None:
        self.elapsed_ms = 0.0

    async def search(self, **kwargs):
        start = time.perf_counter()
        hits = await self._inner.search(**kwargs)
        self.elapsed_ms += (time.perf_counter() - start) * 1000
        return hits

    def __getattr__(self, name):
        return getattr(self._inner, name)


def load_manifest() -> dict:
    """读取语料清单；问题集指纹不符说明标注与语料不同步，直接拒绝运行。"""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    actual = hashlib.sha256(QUESTIONS_PATH.read_bytes()).hexdigest()
    if actual != manifest["questions_sha256"]:
        raise SystemExit("问题集与清单指纹不一致：请重新运行 scripts/gen_rag_eval_corpus.py 生成")
    return manifest


def load_questions() -> list[dict]:
    """读取问题集，一行一个问题。"""
    return [
        json.loads(line)
        for line in QUESTIONS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def eval_settings(*, no_proxy: bool):
    """构造评测专用配置：只覆盖集合名，其余沿用根目录 .env。"""
    if no_proxy:
        # httpx 会读 Windows 注册表里的系统代理，代理没开时连接会被拒。
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
    settings = backend().get_settings()
    return settings.model_copy(update={"vector_store_collection": EVAL_COLLECTION})


async def drop_collection(settings) -> int:
    """删除评测集合，返回 HTTP 状态码；404 视为已不存在。"""
    import httpx

    base = settings.vector_store_url.rstrip("/")
    key = settings.vector_store_api_key
    headers = {"api-key": key.get_secret_value()} if key else {}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.delete(
            f"{base}/collections/{settings.vector_store_collection}", headers=headers
        )
    return response.status_code


def corpus_chunk_texts(manifest: dict, settings) -> list[str]:
    """按运行期切分参数还原语料的所有 chunk 原文。

    用于统计 BM25 的 idf，也用于打印标注对照表；两条路径必须与索引时的切分完全
    一致，否则 idf 与稀疏向量就对不上。
    """
    mods = backend()
    texts: list[str] = []
    for document in manifest["documents"]:
        raw = (CORPUS_DIR / document["path"]).read_bytes()
        parsed = mods.parse_document(raw, file_type=document["file_type"])
        if parsed.pages is None:
            parts = [mods.chunk_document(parsed.lines, max_chars=settings.chunk_max_chars)]
        else:
            parts = [
                mods.chunk_document(page, max_chars=settings.chunk_max_chars)
                for page in parsed.pages
            ]
        texts.extend(chunk.text for part in parts for chunk in part)
    return texts


def build_sparse_encoder(manifest: dict, settings):
    """按评测语料统计 idf，并把统计结果留档以便复现。

    IDF 决定稀疏通道的语义，因此它和结果一起落盘：换一份语料统计出的 idf 会得到
    不同的混合检索行为，不能把两次结果当成同一配置的比较。
    """
    mods = backend()
    texts = corpus_chunk_texts(manifest, settings)
    encoder = mods.Bm25Encoder.fit(texts)
    payload = {
        "corpus_questions_sha256": manifest["questions_sha256"],
        "chunk_count": len(texts),
        "avg_doc_len": encoder.avg_doc_len,
        "k1": encoder.k1,
        "b": encoder.b,
        "default_idf": encoder.default_idf,
        "idf": encoder.idf,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "idf.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"BM25 idf 统计完成：{len(texts)} 个 chunk，{len(encoder.idf)} 个词项，已留档")
    return encoder


async def index_corpus(settings, manifest: dict, sparse_encoder=None) -> int:
    """把语料逐文档逐版本索引进评测集合；已索引的版本跳过。"""
    mods = backend()
    store = mods.get_vector_store(settings)
    await store.ensure_collection()

    if manifest["chunk_max_chars"] != settings.chunk_max_chars:
        raise SystemExit(
            f"切分参数不一致：标注按 {manifest['chunk_max_chars']} 生成，"
            f"当前配置为 {settings.chunk_max_chars}；chunk_id 会漂移，拒绝运行"
        )

    indexed = 0
    for document in manifest["documents"]:
        raw = (CORPUS_DIR / document["path"]).read_bytes()
        try:
            await mods.index_document(
                raw,
                user_id=document["user_id"],
                document_id=document["document_id"],
                version=document["version"],
                file_type=document["file_type"],
                settings=settings,
                sparse_encoder=sparse_encoder,
            )
            indexed += 1
            print(f"  索引 {document['document_id']} v{document['version']} -> OK")
        except mods.VectorStoreError as error:
            # 版本不可原地重建；重复运行时把它当作"已索引"继续。
            if "已有向量" not in str(error):
                raise
            print(f"  跳过 {document['document_id']} v{document['version']}（已索引）")
    return indexed


def result_path(strategy: str) -> Path:
    """结果文件按策略区分，便于同一进程内多次运行互不覆盖。"""
    return RESULTS_DIR / f"{strategy}.jsonl"


def question_to_payload(question: dict) -> dict:
    """把问题转成检索调用所需的参数与期望，避免各处重复解析。"""
    return {
        "user_id": question["user_id"],
        "query": question["query"],
        "document_versions": question["document_versions"],
    }


async def retrieve_once(source, payload: dict, *, top_k: int | None, mods):
    """对一条问题执行一次检索。

    失败如实抛出：生产部署的向量库是同机本地服务，不存在需要掩盖的跨网抖动；
    隐藏失败或自动重试只会让评测结果失去可信度。
    """
    return await source.retrieve_evidence(
        user_id=payload["user_id"],
        query=payload["query"],
        document_versions=[
            mods.DocumentVersion.model_validate(item) for item in payload["document_versions"]
        ],
        top_k=top_k,
    )


async def evaluate(
    settings,
    questions: list[dict],
    strategy: str,
    *,
    resume: bool,
    top_k: int | None,
    sparse_encoder=None,
) -> list:
    """逐条检索并把结果增量写入 JSONL；中断后可用 --resume 续跑。"""
    mods = backend()
    path = result_path(strategy)
    path.parent.mkdir(parents=True, exist_ok=True)

    done: set[str] = set()
    if resume and path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                done.add(json.loads(line)["question_id"])
        print(f"  续跑：已完成 {len(done)} 条")
    elif path.exists():
        # 不续跑时必须清空旧结果，否则会与上一轮的行混在一起、指标被重复计数。
        path.unlink()

    embedder = TimedEmbedder(mods.get_embedding_provider(settings))
    store = TimedStore(mods.get_vector_store(settings))
    source = mods.QdrantEvidenceSource(
        settings, embedder=embedder, store=store, sparse_encoder=sparse_encoder
    )
    config_versions: set[str] = set()

    with path.open("a", encoding="utf-8", newline="\n") as sink:
        for question in questions:
            if question["id"] in done:
                continue
            payload = question_to_payload(question)
            embedder.reset()
            store.reset()
            started = time.perf_counter()
            result = await retrieve_once(source, payload, top_k=top_k, mods=mods)
            total_ms = (time.perf_counter() - started) * 1000
            config_versions.add(result.retrieval_config_version)
            sink.write(
                json.dumps(
                    {
                        "question_id": question["id"],
                        "kind": question["kind"],
                        "config_version": result.retrieval_config_version,
                        "returned": [
                            {"chunk_id": item.chunk_id, "score": item.retrieval_score}
                            for item in result.evidence
                        ],
                        "total_ms": total_ms,
                        "embedding_ms": embedder.elapsed_ms,
                        "search_ms": store.elapsed_ms,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            sink.flush()
            print(f"  {question['id']}: 命中 {len(result.evidence)} 条（{total_ms:.0f} ms）")

    if len(config_versions) > 1:
        raise SystemExit(f"同一策略出现多个配置版本，结果不可比：{sorted(config_versions)}")
    return load_results(strategy, questions)


def load_results(strategy: str, questions: list[dict]) -> list:
    """把增量结果读回成指标模块的 QueryResult 列表。"""
    mods = backend()
    by_id = {question["id"]: question for question in questions}
    results = []
    for line in result_path(strategy).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        question = by_id[row["question_id"]]
        results.append(
            mods.eval_metrics.QueryResult(
                question_id=row["question_id"],
                kind=row["kind"],
                expect=question.get("expect"),
                relevant=tuple(question["relevant_chunk_ids"]),
                forbidden=tuple(question.get("forbidden_chunk_ids", [])),
                returned=tuple(item["chunk_id"] for item in row["returned"]),
                total_ms=row["total_ms"],
                embedding_ms=row["embedding_ms"],
                search_ms=row["search_ms"],
            )
        )
    return results


def latency_note(settings) -> str | None:
    """说明耗时是在什么环境测的。

    生产部署的向量库是同机本地服务；本轮若跑在临时云端环境，延迟会被网络往返放大，
    直接当成部署开销会误导决策，因此必须在报告里写清口径。
    """
    url = settings.vector_store_url
    is_local = any(host in url for host in ("127.0.0.1", "localhost", "::1"))
    if is_local:
        return None
    return (
        f"耗时为云端 Qdrant（{settings.vector_store_collection} 所在集群）实测值，"
        "含公网往返，不代表本地部署开销"
    )


def dump_labels() -> None:
    """打印问题与目标 chunk 原文的对照表，供人工复核标注。"""
    mods = backend()
    manifest = load_manifest()
    settings = mods.get_settings()

    chunk_text: dict[str, str] = {}
    for document in manifest["documents"]:
        raw = (CORPUS_DIR / document["path"]).read_bytes()
        parsed = mods.parse_document(raw, file_type=document["file_type"])
        if parsed.pages is None:
            parts = [mods.chunk_document(parsed.lines, max_chars=settings.chunk_max_chars)]
        else:
            parts = [
                mods.chunk_document(page, max_chars=settings.chunk_max_chars)
                for page in parsed.pages
            ]
        ordinal = 0
        for part in parts:
            for chunk in part:
                chunk_id = mods.make_chunk_id(document["document_id"], document["version"], ordinal)
                chunk_text[chunk_id] = chunk.text
                ordinal += 1

    for question in load_questions():
        print(f"\n[{question['id']}] {question['kind']} / {question['style']}")
        print(f"  问：{question['query']}")
        print(f"  依据：{question['note']}")
        for chunk_id in question["relevant_chunk_ids"]:
            text = chunk_text.get(chunk_id, "<未找到>")
            print(f"  目标 chunk：{text.strip()[:160]}…")
        for chunk_id in question.get("forbidden_chunk_ids", []):
            text = chunk_text.get(chunk_id, "<未找到>")
            print(f"  禁用 chunk：{text.strip()[:160]}…")


def main() -> None:
    parser = argparse.ArgumentParser(description="M2 三档检索策略评测运行器")
    parser.add_argument("--rebuild", action="store_true", help="删除并重建评测集合后索引")
    parser.add_argument("--index-only", action="store_true", help="只索引，不评测")
    parser.add_argument("--evaluate-only", action="store_true", help="跳过索引，只评测")
    parser.add_argument(
        "--strategies",
        nargs="+",
        default=["dense"],
        help="要评测的策略；hybrid / hybrid_rerank 待 S3/S4 实现",
    )
    parser.add_argument("--resume", action="store_true", help="跳过已完成的题目续跑")
    parser.add_argument("--top-k", type=int, default=None, help="覆盖 retrieval_top_k")
    parser.add_argument("--no-proxy", action="store_true", help="绕过系统代理直连外部服务")
    parser.add_argument("--dump-labels", action="store_true", help="打印标注对照表后退出")
    args = parser.parse_args()

    if args.dump_labels:
        dump_labels()
        return

    unsupported = [name for name in args.strategies if name not in ("dense", "hybrid")]
    if unsupported:
        raise SystemExit(
            f"策略 {unsupported} 尚未实现（Rerank 适配器属 S4）；当前可评测 dense 与 hybrid"
        )

    settings = eval_settings(no_proxy=args.no_proxy)
    manifest = load_manifest()

    async def run() -> None:
        if args.rebuild:
            status = await drop_collection(settings)
            print(f"删除评测集合 {EVAL_COLLECTION} -> HTTP {status}")
        # 稀疏向量一并写入：三档策略共用同一个索引，才能保证结果可比。
        encoder = build_sparse_encoder(manifest, settings)
        if not args.evaluate_only:
            indexed = await index_corpus(settings, manifest, sparse_encoder=encoder)
            print(f"索引完成：{indexed} 个文档版本")
        if args.index_only:
            return
        questions = load_questions()
        for strategy in args.strategies:
            print(f"\n开始评测策略 {strategy}（{len(questions)} 条问题）")
            strategy_settings = settings.model_copy(update={"retrieval_strategy": strategy})
            results = await evaluate(
                strategy_settings,
                questions,
                strategy,
                resume=args.resume,
                top_k=args.top_k,
                sparse_encoder=encoder,
            )
            mods = backend()
            summary = mods.eval_metrics.summarize(results)
            config_version = json.loads(
                result_path(strategy).read_text(encoding="utf-8").splitlines()[0]
            )["config_version"]
            report = mods.eval_metrics.render_markdown(
                summary,
                strategy=strategy,
                config_version=config_version,
                latency_note=latency_note(settings),
            )
            (RESULTS_DIR / f"{strategy}.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            (RESULTS_DIR / f"{strategy}.md").write_text(
                report + "\n", encoding="utf-8", newline="\n"
            )
            print("\n" + report)

    asyncio.run(run())


if __name__ == "__main__":
    main()
