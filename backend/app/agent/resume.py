"""B Worker 的计划确认恢复入口，不调用模型、不创建正式任务。"""

from dataclasses import dataclass

from app.agent.checkpoints import CheckpointStore
from app.agent.errors import AgentError


@dataclass(frozen=True)
class ResumeOutcome:
    status: str
    result_type: str
    result_id: str


def confirm_saved_plan(*, user_id, execution_id, resume_input, confirmed_plan, store):
    """必须先核对 B 已提交的确认事务，再完成 D 的快照。"""
    state = store.load(user_id, execution_id)
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
    """仅支持计划确认；追问恢复尚未接通，明确拒绝而非伪成功。"""
    if resume_input.get("kind") != "plan_confirmation":
        raise AgentError("RESUME_NOT_SUPPORTED", "资料追问恢复尚未集成。")
    # 惰性导入 B PR #28 的权威查询，未合入时不会用本地快照冒充业务确认。
    from app.services.plans import get_plan

    store = CheckpointStore(settings.local_storage_dir)
    state = store.load(user_id, execution_id)
    confirmed_plan = get_plan(settings, user_id, state["plan_id"])
    return confirm_saved_plan(
        user_id=user_id,
        execution_id=execution_id,
        resume_input=resume_input,
        confirmed_plan=confirmed_plan,
        store=store,
    )
