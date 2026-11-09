"""D 的本地持久化快照；业务状态和正式任务仍归 B。Refs #26。"""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from app.agent.errors import AgentError


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class CheckpointStore:
    """单机 SQLite 原子认领；不得把本地路径当成多机共享存储。"""

    def __init__(self, storage_dir: Path):
        directory = Path(storage_dir) / "agent"
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "checkpoints.sqlite3"
        with self.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS checkpoints (execution TEXT PRIMARY KEY, body TEXT)"
            )

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def read(db, user_id, execution_id):
        row = db.execute(
            "SELECT body FROM checkpoints WHERE execution=?", (execution_id,)
        ).fetchone()
        if row is None:
            raise AgentError("CHECKPOINT_MISSING", "找不到保存的执行进度。")
        state = json.loads(row[0])
        if state["user_id"] != user_id:
            raise AgentError("CHECKPOINT_MISSING", "找不到保存的执行进度。")
        return state

    @staticmethod
    def write(db, state):
        db.execute(
            "UPDATE checkpoints SET body=? WHERE execution=?",
            (json.dumps(state, ensure_ascii=False), state["execution_id"]),
        )

    def load(self, user_id, execution_id):
        with self.connection() as db:
            return self.read(db, user_id, execution_id)

    def claim(self, *, user_id, execution_id, fingerprint, plan_id, budget):
        if not user_id or not execution_id:
            raise AgentError("INVALID_INPUT", "保存进度需要有效的用户和执行编号。")
        with self.connection() as db:
            existing = db.execute(
                "SELECT 1 FROM checkpoints WHERE execution=?", (execution_id,)
            ).fetchone()
            if existing:
                state = self.read(db, user_id, execution_id)
                if state["fingerprint"] != fingerprint:
                    raise AgentError("STALE_CHECKPOINT", "执行来源已变化，请重新创建计划。")
                if state["status"] not in {"waiting", "succeeded"}:
                    raise AgentError(
                        "CHECKPOINT_INCOMPLETE", "上次生成尚未完成，请检查任务后重新创建。"
                    )
                return state, False
            state = dict(
                user_id=user_id,
                execution_id=execution_id,
                fingerprint=fingerprint,
                plan_id=plan_id,
                status="generating",
                budget=budget,
                checkpoint_id=f"chk_{uuid4().hex}",
                interrupt_id=f"int_{uuid4().hex}",
            )
            db.execute(
                "INSERT INTO checkpoints VALUES (?, ?)",
                (execution_id, json.dumps(state, ensure_ascii=False)),
            )
            return state, True

    def park(self, user_id, execution_id, tasks):
        with self.connection() as db:
            state = self.read(db, user_id, execution_id)
            if state["status"] != "generating":
                raise AgentError("STALE_CHECKPOINT", "执行进度已变化。")
            state.update(status="waiting", draft_tasks=tasks)
            self.write(db, state)
            return state

    def finish(self, user_id, execution_id, resume_input):
        with self.connection() as db:
            state = self.read(db, user_id, execution_id)
            if any(resume_input.get(k) != state[k] for k in ("interrupt_id", "checkpoint_id")):
                raise AgentError("STALE_CHECKPOINT", "回答不属于当前等待确认的进度。")
            answer = resume_input.get("answer")
            if not isinstance(answer, dict) or answer.get("confirmed") is not True:
                raise AgentError("INVALID_INPUT", "恢复需要明确的计划确认。")
            if resume_input.get("kind") != "plan_confirmation" or answer != {
                "confirmed": True,
                "plan_id": state["plan_id"],
            }:
                raise AgentError("INVALID_INPUT", "恢复需要已保存的计划确认。")
            fingerprint = digest(
                {
                    k: resume_input.get(k)
                    for k in (
                        "kind",
                        "answer",
                        "interrupt_id",
                        "checkpoint_id",
                        "document_versions",
                    )
                }
            )
            if state["status"] == "succeeded":
                if state["resume_fingerprint"] != fingerprint:
                    raise AgentError("INPUT_CONFLICT", "这次确认与已处理的输入不同。")
                return state
            if state["status"] != "waiting":
                raise AgentError("STALE_CHECKPOINT", "任务不在等待确认状态。")
            state.update(status="succeeded", resume_fingerprint=fingerprint)
            self.write(db, state)
            return state
