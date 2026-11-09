"""B Worker 的计划确认恢复入口，不调用模型、不创建正式任务。"""

from dataclasses import dataclass

from app.agent.checkpoints import CheckpointStore
from app.agent.errors import AgentError


@dataclass(frozen=True)
class ResumeOutcome:
    status: str
    result_type: str | None = None
    result_id: str | None = None
    pending_input: dict | None = None


def confirm_saved_plan(*, user_id, execution_id, resume_input, confirmed_plan, store):
    """必须先核对 B 已提交的确认事务，再完成 D 的快照。"""
    state = store.load(user_id, execution_id)
    if state["status"] == "invalidated":
        raise AgentError("STALE_CHECKPOINT", "计划来源已删除，进度不可恢复。")
    if (
        resume_input.get("user_id", user_id) != user_id
        or resume_input.get("execution_id", execution_id) != execution_id
    ):
        raise AgentError("INVALID_INPUT", "恢复输入不属于当前任务。")
    if (
        not confirmed_plan
        or confirmed_plan.get("id") != state["plan_id"]
        or confirmed_plan.get("user_id") != user_id
        or confirmed_plan.get("execution_id") != execution_id
        or confirmed_plan.get("status") != "confirmed"
        or confirmed_plan.get("draft_tasks") != state["draft_tasks"]
    ):
        raise AgentError("PLAN_NOT_CONFIRMED", "计划未确认或内容发生变化，不能恢复。")
    store.finish(user_id, execution_id, resume_input)
    return ResumeOutcome(status="succeeded", result_type="plan", result_id=state["plan_id"])


async def resume_execution(*, user_id, execution_id, resume_input, settings):
    """恢复仅使用 B 已保存的输入；等待与完成交由 Worker 更新。"""
    if resume_input.get("kind") == "clarification":
        from app.services.agent_bridge import resume_analysis

        return await resume_analysis(settings, user_id, execution_id, resume_input)
    if resume_input.get("kind") != "plan_confirmation":
        raise AgentError("INVALID_INPUT", "不支持的恢复类型。")
    # 惰性导入 B PR #28 的权威查询，未合入时不会用本地快照冒充业务确认。
    from app.services.agent_bridge import validate_sources
    from app.services.analyses import get_analysis
    from app.services.inputs import load_input_by_id
    from app.services.jobs import load_job
    from app.services.plans import get_plan

    store = CheckpointStore(settings.local_storage_dir)
    state = store.load(user_id, execution_id)
    if state["status"] == "invalidated":
        raise AgentError("STALE_CHECKPOINT", "计划来源已删除，进度不可恢复。")
    if load_input_by_id(settings, user_id, resume_input.get("id", "")) != resume_input:
        raise AgentError("INVALID_INPUT", "确认输入未保存在当前用户记录中。")
    confirmed_plan = get_plan(settings, user_id, state["plan_id"])
    analysis = (
        get_analysis(settings, user_id, confirmed_plan["analysis_id"]) if confirmed_plan else None
    )
    if not analysis or analysis.get("status") != "ready" or analysis.get("stale", False):
        raise AgentError("STALE_CHECKPOINT", "计划来源分析不可用。")
    job = load_job(settings, user_id, analysis["job_id"])
    if job is None or job.version != analysis["job_version"]:
        raise AgentError("STALE_CHECKPOINT", "岗位版本已变化。")
    from app.schemas.analysis import DocumentVersion

    validate_sources(
        settings,
        user_id,
        [DocumentVersion.model_validate(v) for v in analysis["document_versions"]],
        [],
    )
    return confirm_saved_plan(
        user_id=user_id,
        execution_id=execution_id,
        resume_input=resume_input,
        confirmed_plan=confirmed_plan,
        store=store,
    )
