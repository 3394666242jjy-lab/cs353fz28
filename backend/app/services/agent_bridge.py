"""B↔D 接线：业务权限与结果保存归 B，图和快照归 D。Refs #26。"""

from app.agent.checkpoints import CheckpointStore
from app.agent.clarification import ClarificationFlow
from app.agent.errors import AgentError
from app.agent.pipeline import LangGraphAnalysisAgent
from app.schemas.analysis import DocumentVersion
from app.services.documents import get_document, list_document_versions


def validate_sources(settings, user_id, pinned, new_ids, *, input_versions=None):
    """当前权限和版本以 MongoDB 为准；补充版本必须匹配已保存的恢复输入。"""
    versions = {v.document_id: v.version for v in pinned}
    if len(versions) != len(pinned) or len(set(new_ids)) != len(new_ids):
        raise AgentError("INVALID_INPUT", "资料列表不能重复。")
    expected = {v["document_id"]: v["version"] for v in input_versions or []}
    if input_versions is not None and set(expected) != set(new_ids):
        raise AgentError("STALE_CHECKPOINT", "补充资料与已保存输入不一致。")
    for document_id in dict.fromkeys([*versions, *new_ids]):
        doc = get_document(settings, user_id, document_id)
        if doc is None or doc.status != "ready":
            raise AgentError("DOCUMENT_NOT_READY", "资料不可访问、已删除或未就绪。")
        if document_id not in new_ids and doc.current_version != versions[document_id]:
            raise AgentError("STALE_CHECKPOINT", "分析引用的资料版本已变化。")
        if document_id in new_ids:
            if input_versions is not None and expected[document_id] != doc.current_version:
                raise AgentError("STALE_CHECKPOINT", "补充资料在提交后发生了变化。")
            versions[document_id] = doc.current_version
        rows = list_document_versions(settings, user_id, document_id)
        if not any(
            v["version"] == versions[document_id] and v.get("status") == "ready" for v in rows
        ):
            raise AgentError("DOCUMENT_NOT_READY", "资料版本未完成索引。")
    if not 1 <= len(versions) <= 20:
        raise AgentError("INVALID_INPUT", "分析需要一到二十份有效资料。")
    return [DocumentVersion(document_id=k, version=v) for k, v in versions.items()]


class M2AnalysisAgent(LangGraphAnalysisAgent):
    """保留 M1 接口，并为真实 Worker 提供等待输入结果。"""

    def __init__(self, model, settings, business_settings):
        super().__init__(model, settings)
        self.business_settings = business_settings

    def flow(self, retriever, *, input_versions=None):
        async def validate(user, pinned, new_ids):
            return validate_sources(
                self.business_settings, user, pinned, new_ids, input_versions=input_versions
            )

        return ClarificationFlow(
            CheckpointStore(self.business_settings.local_storage_dir),
            self.model,
            retriever,
            self.settings,
            validate,
        )

    async def run_analysis_or_pause(
        self, *, user_id, execution_id, analysis_id, job, document_versions, retriever
    ):
        flow = self.flow(retriever)
        outcome = await flow.start(
            user_id=user_id,
            execution_id=execution_id,
            analysis_id=analysis_id,
            job=job,
            document_versions=document_versions,
        )
        validate_sources(self.business_settings, user_id, outcome.run.result.document_versions, [])
        return outcome


async def resume_analysis(settings, user_id, execution_id, resume_input):
    """B 侧保存权威分析结果后，D 才能报告业务成功。"""
    from app.agent.config import AgentSettings
    from app.agent.provider import ChatCompletionModel
    from app.agent.resume import ResumeOutcome
    from app.services.analyses import get_analysis, save_analysis_result
    from app.services.inputs import load_input_by_id
    from app.services.jobs import load_job
    from app.worker import _build_retriever

    stored = load_input_by_id(settings, user_id, resume_input.get("id", ""))
    if stored is None or stored != resume_input or stored.get("execution_id") != execution_id:
        raise AgentError("INVALID_INPUT", "恢复必须使用当前用户已保存的回答。")
    agent_settings = AgentSettings()
    agent = M2AnalysisAgent(ChatCompletionModel(agent_settings), agent_settings, settings)
    flow = agent.flow(
        _build_retriever(settings), input_versions=stored.get("document_versions", [])
    )
    with flow.store.connection() as db:
        state = flow.load(db, user_id, execution_id)
    row = get_analysis(settings, user_id, state["analysis_id"])
    if row is None or row["status"] not in {"pending", "ready"}:
        raise AgentError("ANALYSIS_MISSING", "分析来源不可用。")
    job = load_job(settings, user_id, row["job_id"])
    if job is None or job.version != row["job_version"]:
        raise AgentError("STALE_CHECKPOINT", "岗位来源发生变化。")
    outcome = await flow.resume(user_id=user_id, execution_id=execution_id, resume_input=stored)
    validate_sources(settings, user_id, outcome.run.result.document_versions, [])
    if outcome.status == "waiting_for_input":
        return ResumeOutcome(
            status="waiting_for_input", pending_input=outcome.pending_input.model_dump()
        )
    result = outcome.run.result
    if row["status"] == "ready":
        if row["document_versions"] != [v.model_dump() for v in result.document_versions] or row[
            "matches"
        ] != [m.model_dump() for m in result.matches]:
            raise AgentError("STALE_CHECKPOINT", "已保存的分析与恢复结果不一致。")
    elif not save_analysis_result(
        settings, user_id, row["id"], result, expected_document_versions=row["document_versions"]
    ):
        raise AgentError("ANALYSIS_SAVE_FAILED", "分析保存失败，任务状态已变化。")
    return ResumeOutcome(status="succeeded", result_type="analysis", result_id=row["id"])
