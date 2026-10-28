"""Run the real graph with explicitly synthetic evidence; offline unless opted in."""

import argparse
import asyncio
import getpass
import json
import sys
from pathlib import Path

from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.agent.config import AgentSettings  # noqa: E402
from app.agent.errors import AgentError  # noqa: E402
from app.agent.models import ModelCompletion  # noqa: E402
from app.agent.pipeline import LangGraphAnalysisAgent, get_analysis_agent  # noqa: E402
from app.schemas.analysis import DocumentVersion, Evidence  # noqa: E402
from app.schemas.resources import JobResult  # noqa: E402

REQUIREMENTS = ["有数据库设计经验。", "有缓存优化和测量经验。", "有 Docker 部署经验。"]
DOCUMENT_ID = "synthetic_demo_document"
ROWS = [
    Evidence(
        chunk_id="synthetic_db_chunk",
        document_id=DOCUMENT_ID,
        version=1,
        filename="synthetic-project.md",
        source_type="personal",
        quote="在课程项目中负责数据库表结构、主外键及索引设计。",
        locator={"kind": "text", "line_start": 1, "line_end": 1},
    ),
    Evidence(
        chunk_id="synthetic_cache_chunk",
        document_id=DOCUMENT_ID,
        version=1,
        filename="synthetic-project.md",
        source_type="personal",
        quote="使用 Redis 缓存课程列表；尚未记录优化前后的性能测量。",
        locator={"kind": "text", "line_start": 2, "line_end": 2},
    ),
]


class SyntheticRetriever:
    """Returns declared synthetic text, never Qdrant or a parsed PDF."""

    is_test_double = True
    retrieval_config_version = "synthetic-demo-v1"

    async def retrieve_evidence(self, *, user_id, query, document_versions, top_k=5):
        if user_id != "synthetic_demo_user" or document_versions != [
            DocumentVersion(document_id=DOCUMENT_ID, version=1)
        ]:
            raise AgentError("EVIDENCE_INVALID", "合成演示仅支持其声明的资料快照。")
        keywords = [("数据库", ROWS[0]), ("缓存", ROWS[1])]
        return [row.model_copy(deep=True) for word, row in keywords if word in query][:top_k]


class OfflineScriptedModel:
    """Predictable demonstration responses, explicitly not real model inference."""

    is_test_double = True

    async def complete(self, *, system, payload, output_schema):
        if output_schema.__name__ == "RequirementBatch":
            data = {
                "requirements": [{"text": text, "category": "required"} for text in REQUIREMENTS]
            }
        else:
            items = []
            for requirement in payload["requirements"]:
                text = requirement["text"]
                status = (
                    "supported"
                    if "数据库" in text
                    else "partial"
                    if "缓存" in text
                    else "not_evidenced"
                )
                items.append(
                    {
                        "requirement_id": requirement["id"],
                        "status": status,
                        "evidence_ids": [e["chunk_id"] for e in requirement["evidence"]],
                        "explanation": {
                            "supported": "合成原文记录了表结构和索引设计。",
                            "partial": "合成原文提到缓存，但缺少性能测量。",
                            "not_evidenced": "当前合成资料未体现 Docker 部署。",
                        }[status],
                        "missing_information": []
                        if status == "supported"
                        else [
                            "请补充性能测量记录。" if status == "partial" else "请补充部署经历。"
                        ],
                    }
                )
            data = {"assessments": items}
        return ModelCompletion(
            content=json.dumps(data, ensure_ascii=False),
            model_version="scripted-demo-not-a-model",
        )


async def run(args):
    destination = args.output.resolve() if args.output else None
    if destination is not None and not destination.is_relative_to(
        (ROOT / "storage/agent").resolve()
    ):
        raise AgentError("INVALID_OUTPUT_PATH", "演示报告只能保存到 storage/agent/。")
    if args.mode == "live-model":
        if not args.allow_live:
            raise AgentError(
                "LIVE_OPT_IN_REQUIRED", "真实模型会消耗额度；确认后添加 --allow-live。"
            )
        settings = AgentSettings()
        if args.base_url:
            settings.base_url = args.base_url
        if args.model:
            settings.model = args.model
        # Validate endpoint/model before asking for a secret. No key is written to disk.
        settings.model_copy(update={"require_api_key": False}).check_provider()
        if settings.require_api_key and settings.api_key is None:
            if not sys.stdin.isatty():
                raise AgentError(
                    "MODEL_NOT_CONFIGURED", "请在交互终端输入密钥，或本机配置环境变量。"
                )
            settings.api_key = SecretStr(getpass.getpass("API key (hidden; not saved): "))
        agent = get_analysis_agent(settings)
    else:
        # Ignore .env entirely: offline mode must remain reproducible and make no HTTP calls.
        agent = LangGraphAnalysisAgent(OfflineScriptedModel(), AgentSettings(_env_file=None))
    report = await agent.run_analysis_with_details(
        user_id="synthetic_demo_user",
        job=JobResult(
            id="synthetic_demo_job",
            title="合成后端岗位",
            raw_jd="\n".join(REQUIREMENTS),
            version=1,
        ),
        document_versions=[DocumentVersion(document_id=DOCUMENT_ID, version=1)],
        retriever=SyntheticRetriever(),
    )
    output = {
        "mode": args.mode,
        "real_langgraph": True,
        "real_model": args.mode == "live-model",
        "real_rag": False,
        "pdf_parsing_verified": False,
        "notice": "合成资料演示；不代表 B/C 联调、PDF 解析或模型效果评测。",
        **report.model_dump(mode="json"),
    }
    encoded = json.dumps(output, ensure_ascii=False, indent=2)
    if destination is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["offline", "live-model"], default="offline")
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except AgentError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
