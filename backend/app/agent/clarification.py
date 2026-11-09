"""M2 追问阶段快照；B 注入权限复核，接收结果后负责业务持久化。Refs #26。"""

import asyncio
import json
import time
from dataclasses import dataclass
from uuid import uuid4

from pydantic import Field, StrictBool, ValidationError, model_validator

from app.agent.checkpoints import CheckpointStore, digest
from app.agent.errors import AgentError
from app.agent.models import AnalysisRun
from app.agent.pipeline import LangGraphAnalysisAgent
from app.schemas.analysis import DocumentVersion
from app.schemas.common import ContractModel
from app.schemas.resources import JobResult, PendingInput


class ClarificationAnswer(ContractModel):
    text: str = Field(default="", max_length=8000)
    continue_without_evidence: StrictBool = False
    document_ids: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_answer(self):
        if not self.text and not self.continue_without_evidence and not self.document_ids:
            raise ValueError("empty answer")
        if any(not s.strip() for s in self.document_ids) or len(set(self.document_ids)) != len(
            self.document_ids
        ):
            raise ValueError("invalid documents")
        if self.continue_without_evidence and self.document_ids:
            raise ValueError("ambiguous answer")
        return self


@dataclass(frozen=True)
class ClarificationOutcome:
    status: str
    run: AnalysisRun
    pending_input: PendingInput | None


class ClarificationFlow:
    """跨进程恢复步骤快照；不是 LangGraph 原生中断适配器。"""

    def __init__(self, store: CheckpointStore, model, retriever, settings, validate_sources):
        self.store, self.model, self.retriever = store, model, retriever
        self.settings, self.validate_sources = settings, validate_sources
        with store.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS dialogues (execution TEXT PRIMARY KEY, body TEXT)"
            )

    def load(self, db, user, execution):
        row = db.execute("SELECT body FROM dialogues WHERE execution=?", (execution,)).fetchone()
        if row is None or json.loads(row[0])["user_id"] != user:
            raise AgentError("CHECKPOINT_MISSING", "找不到保存的追问进度。")
        return json.loads(row[0])

    @staticmethod
    def save(db, state):
        db.execute(
            "UPDATE dialogues SET body=? WHERE execution=?",
            (json.dumps(state, ensure_ascii=False), state["execution_id"]),
        )

    def reserve(self, user, execution, *, model_requests=0, tools=0, seconds=0):
        with self.store.connection() as db:
            state = self.load(db, user, execution)
            budget = state["budget"]
            limits = state["limits"]
            if (
                budget["model_requests"] + model_requests > limits["model_requests"]
                or budget["tools"] + tools > limits["tools"]
                or budget["seconds"] + seconds > limits["seconds"]
            ):
                raise AgentError("BUDGET_EXCEEDED", "累计执行预算已用完，不能通过恢复重置。")
            budget["model_requests"] += model_requests
            budget["tools"] += tools
            budget["seconds"] += seconds
            self.save(db, state)

    async def analyse(self, user, execution, job, versions):
        flow = self

        class BudgetModel:
            is_test_double = getattr(flow.model, "is_test_double", False)

            async def complete(self, **kwargs):
                return await invoke(
                    flow.model.complete,
                    flow.settings.model_timeout_seconds,
                    model_requests=flow.settings.max_attempts,
                    **kwargs,
                )

        class BudgetRetriever:
            is_test_double = getattr(flow.retriever, "is_test_double", False)

            @property
            def retrieval_config_version(self):
                return getattr(flow.retriever, "retrieval_config_version", None)

            async def retrieve_evidence(self, **kwargs):
                return await invoke(
                    flow.retriever.retrieve_evidence,
                    flow.settings.tool_timeout_seconds,
                    tools=1,
                    **kwargs,
                )

        async def invoke(call, timeout, model_requests=0, tools=0, **kwargs):
            flow.reserve(
                user, execution, model_requests=model_requests, tools=tools, seconds=timeout
            )
            started = time.monotonic()
            try:
                async with asyncio.timeout(timeout):
                    return await call(**kwargs)
            finally:
                # 归还未使用的预留秒数；进程崩溃时预留保留，次数不退款。
                elapsed = min(timeout, time.monotonic() - started)
                with flow.store.connection() as db:
                    state = flow.load(db, user, execution)
                    state["budget"]["seconds"] = max(
                        0, state["budget"]["seconds"] - timeout + elapsed
                    )
                    flow.save(db, state)

        return await LangGraphAnalysisAgent(BudgetModel(), self.settings).run_analysis_with_details(
            user_id=user,
            job=JobResult.model_validate(job),
            document_versions=[DocumentVersion.model_validate(v) for v in versions],
            retriever=BudgetRetriever(),
        )

    @staticmethod
    def outcome(state):
        return ClarificationOutcome(
            status=state["status"],
            run=AnalysisRun.model_validate(state["run"]),
            pending_input=PendingInput.model_validate(state["pending"])
            if state.get("pending")
            else None,
        )

    def complete_segment(
        self, user, execution, run, *, force_question=False, input_hash=None, consumed=None
    ):
        with self.store.connection() as db:
            state = self.load(db, user, execution)
            missing = [m.requirement for m in run.result.matches if m.status != "supported"]
            state.update(
                run=run.model_dump(mode="json"),
                status="waiting_for_input" if missing or force_question else "succeeded",
            )
            state["pending"] = (
                dict(
                    kind="clarification",
                    interrupt_id=f"int_{uuid4().hex}",
                    checkpoint_id=f"chk_{uuid4().hex}",
                    question="请上传并索引能证明相关经历的资料，或选择按现有资料继续。",
                )
                if missing or force_question
                else None
            )
            if consumed:
                state["replay"] = {"ids": consumed, "hash": input_hash}
            self.save(db, state)
            return self.outcome(state)

    async def start(self, *, user_id, execution_id, job, document_versions):
        if not user_id or not execution_id:
            raise AgentError("INVALID_INPUT", "追问需要用户和执行编号。")
        versions = await self.validate_sources(user_id, document_versions, [])
        payload = dict(job=job.model_dump(mode="json"), versions=[v.model_dump() for v in versions])
        with self.store.connection() as db:
            if db.execute("SELECT 1 FROM dialogues WHERE execution=?", (execution_id,)).fetchone():
                state = self.load(db, user_id, execution_id)
                if state["fingerprint"] != digest(payload):
                    raise AgentError("STALE_CHECKPOINT", "分析输入已经变化。")
                if state["status"] == "running":
                    raise AgentError("CHECKPOINT_INCOMPLETE", "上次分析未完成，不能自动重跑。")
                return self.outcome(state)
            state = dict(
                user_id=user_id,
                execution_id=execution_id,
                fingerprint=digest(payload),
                **payload,
                status="running",
                rounds=0,
                budget=dict(model_requests=0, tools=0, seconds=0),
                limits=dict(
                    model_requests=self.settings.max_cumulative_model_requests,
                    tools=self.settings.max_tool_calls,
                    seconds=self.settings.total_timeout_seconds,
                    rounds=self.settings.max_clarification_rounds,
                ),
            )
            db.execute("INSERT INTO dialogues VALUES (?, ?)", (execution_id, json.dumps(state)))
        run = await self.analyse(user_id, execution_id, payload["job"], payload["versions"])
        return self.complete_segment(user_id, execution_id, run)

    async def resume(self, *, user_id, execution_id, resume_input):
        if (
            resume_input.get("user_id", user_id) != user_id
            or resume_input.get("execution_id", execution_id) != execution_id
        ):
            raise AgentError("INVALID_INPUT", "恢复输入不属于当前任务。")
        if resume_input.get("kind") != "clarification":
            raise AgentError("INVALID_INPUT", "不是资料追问输入。")
        try:
            answer = ClarificationAnswer.model_validate(resume_input.get("answer"))
        except (ValidationError, TypeError):
            raise AgentError("INVALID_INPUT", "补充回答格式不正确或超出允许长度。") from None
        ids = {k: resume_input.get(k) for k in ("interrupt_id", "checkpoint_id")}
        fingerprint = digest(answer.model_dump())
        with self.store.connection() as db:
            before = self.load(db, user_id, execution_id)
        versions = await self.validate_sources(
            user_id,
            [DocumentVersion.model_validate(v) for v in before["versions"]],
            answer.document_ids,
        )
        retained = {
            (v["document_id"], v["version"])
            for v in before["versions"]
            if v["document_id"] not in answer.document_ids
        }
        if not retained <= {(v.document_id, v.version) for v in versions}:
            raise AgentError("STALE_CHECKPOINT", "未请求更新的原资料版本已经变化。")
        if not answer.document_ids and [v.model_dump() for v in versions] != before["versions"]:
            raise AgentError("STALE_CHECKPOINT", "原资料版本已经变化。")
        if answer.document_ids and not set(answer.document_ids) <= {
            v.document_id for v in versions
        }:
            raise AgentError("DOCUMENT_NOT_READY", "补充资料未通过就绪或权限检查。")
        with self.store.connection() as db:
            state = self.load(db, user_id, execution_id)
            if state.get("replay", {}).get("ids") == ids:
                if state["replay"]["hash"] != fingerprint:
                    raise AgentError("INPUT_CONFLICT", "同一追问不能用不同回答重放。")
                if {(v.document_id, v.version) for v in versions} != {
                    (v["document_id"], v["version"]) for v in state["versions"]
                }:
                    raise AgentError("STALE_CHECKPOINT", "重放时资料版本已变化。")
                if state["status"] == "running":
                    raise AgentError("CHECKPOINT_INCOMPLETE", "恢复尚未完成。")
                return self.outcome(state)
            if state["status"] != "waiting_for_input" or any(
                state["pending"].get(k) != v for k, v in ids.items()
            ):
                raise AgentError("STALE_CHECKPOINT", "不是当前等待回答的进度。")
            if (
                not answer.continue_without_evidence
                and state["rounds"] >= state["limits"]["rounds"]
            ):
                raise AgentError("BUDGET_EXCEEDED", "追问次数已到上限，可按现有资料继续。")
            state["rounds"] += 1
            state.update(status="running", replay={"ids": ids, "hash": fingerprint})
            self.save(db, state)
        run = AnalysisRun.model_validate(state["run"])
        if answer.document_ids:
            run = await self.analyse(
                user_id, execution_id, state["job"], [v.model_dump() for v in versions]
            )
            with self.store.connection() as db:
                current = self.load(db, user_id, execution_id)
                current["versions"] = [v.model_dump() for v in versions]
                self.save(db, current)
        elif answer.continue_without_evidence:
            with self.store.connection() as db:
                current = self.load(db, user_id, execution_id)
                current.update(status="succeeded", pending=None)
                self.save(db, current)
                return self.outcome(current)
        # 自由文本从不传给检索/模型作为证据；保留原结论，重新提示上传。
        return self.complete_segment(
            user_id,
            execution_id,
            run,
            force_question=not answer.document_ids,
            input_hash=fingerprint,
            consumed=ids,
        )
