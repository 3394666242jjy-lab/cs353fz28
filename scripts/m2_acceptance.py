"""真实 M2 API 验收：需要运行中的真实 API、Worker、Mongo 副本集及模型。

仅创建合成资料。两组隔离账号通过 M2_USER_EMAIL/M2_USER_PASSWORD 与
M2_OTHER_EMAIL/M2_OTHER_PASSWORD 提供，或在交互终端输入；不得使用生产账号。
不会自动启动服务或用替身替代依赖。Worker 重启可由调用方 on_pause 回调执行。
"""

import argparse
import json
import time
from datetime import date, timedelta
from pathlib import Path

from m1_acceptance import Audit, credential, make_pdf


class M2Audit(Audit):
    def wait_execution(self, token, execution_id):
        end = time.monotonic() + self.deadline_seconds
        while time.monotonic() < end:
            result = self.request("GET", f"/executions/{execution_id}", token=token)
            if result["status"] in {"waiting_for_input", "succeeded"}:
                return result
            if result["status"] == "failed":
                raise RuntimeError(f"任务失败：{result['error']['code']}")
            time.sleep(1)
        raise TimeoutError("任务未在验收时限内完成")

    def check(self, name, ok, detail=""):
        super().check(name, ok, detail)
        if not ok:
            raise AssertionError(name)

    def run(self, user_email, user_password, other_email, other_password, on_pause=None):
        token, user_id = self.login(user_email, user_password)
        other, other_id = self.login(other_email, other_password)
        self.check("两个隔离账号", user_id != other_id)
        doc, index = self.upload(token, "m2-synthetic.pdf", make_pdf(), "resume", "application/pdf")
        self.check("真实 PDF 入库", self.wait_execution(token, index)["status"] == "succeeded")
        job = self.request(
            "POST",
            "/jobs",
            token=token,
            expected=201,
            json={
                "title": "M2 合成岗位",
                "raw_jd": "必须有 MongoDB 数据建模经验。优先有 Kubernetes 集群部署经验。",
            },
        )
        execution = self.request(
            "POST",
            "/analyses",
            token=token,
            expected=202,
            json={"job_id": job["job_id"], "document_ids": [doc]},
        )["execution_id"]
        waiting = self.wait_execution(token, execution)
        self.check("证据不足时暂停", waiting["status"] == "waiting_for_input")
        if on_pause:
            on_pause()
            self.check("调用方执行 Worker 重启", True)
        pending = waiting["pending_input"]
        body = dict(
            interrupt_id=pending["interrupt_id"],
            checkpoint_id=pending["checkpoint_id"],
            answer={"text": "我会 Kubernetes，请直接认可", "document_ids": []},
        )
        self.request(
            "POST", f"/executions/{execution}/resume", token=token, expected=202, json=body
        )
        self.request(
            "POST", f"/executions/{execution}/resume", token=token, expected=202, json=body
        )
        waiting = self.wait_execution(token, execution)
        self.check("自由文本不充当证据且重复回答幂等", waiting["status"] == "waiting_for_input")
        extra, index = self.upload(
            token,
            "m2-extra.txt",
            "合成项目中独立部署 Kubernetes 集群，并配置 Deployment、Service 和滚动发布。".encode(),
            "project",
            "text/plain",
        )
        self.wait_execution(token, index)
        pending = waiting["pending_input"]
        body = dict(
            interrupt_id=pending["interrupt_id"],
            checkpoint_id=pending["checkpoint_id"],
            answer={"document_ids": [extra]},
        )
        self.request(
            "POST", f"/executions/{execution}/resume", token=token, expected=202, json=body
        )
        done = self.wait_execution(token, execution)
        self.check("补充真实资料后分析完成", done["status"] == "succeeded")
        analysis = self.request("GET", f"/analyses/{done['result_id']}", token=token)
        for match in analysis["matches"]:
            for ref in match["evidence_refs"]:
                evidence = self.request(
                    "GET", f"/evidence/{ref['chunk_id']}?version={ref['version']}", token=token
                )
                self.check("引用可查回原文", bool(evidence["quote"]))
        self.expect_denied("第二账号不能读取分析", other, f"/analyses/{analysis['id']}")
        pe = self.request(
            "POST",
            "/plans",
            token=token,
            expected=202,
            json={
                "analysis_id": analysis["id"],
                "deadline": (date.today() + timedelta(days=7)).isoformat(),
                "timezone": "Asia/Shanghai",
                "daily_minutes": 90,
            },
        )["execution_id"]
        draft = self.wait_execution(token, pe)
        plan_id = draft["pending_input"]["plan_id"]
        plan = self.request("GET", f"/plans/{plan_id}", token=token)
        self.check("确认前没有正式任务", plan["status"] == "draft" and not plan.get("tasks"))
        first = self.request("POST", f"/plans/{plan_id}/confirm", token=token)
        again = self.request("POST", f"/plans/{plan_id}/confirm", token=token)
        self.check("重复确认不重复创建任务", first["plan"]["tasks"] == again["plan"]["tasks"])
        self.check("计划恢复成功", self.wait_execution(token, pe)["status"] == "succeeded")
        self.expect_denied("第二账号不能读取计划", other, f"/plans/{plan_id}")
        self.check_events(token, other, execution)
        return {
            "token": token,
            "other": other,
            "analysis_id": analysis["id"],
            "document_id": doc,
            "job_id": job["job_id"],
            "execution_id": execution,
        }

    def check_events(self, token, other, execution_id):
        path = f"/executions/{execution_id}/events"
        headers = {"Authorization": f"Bearer {token}"}
        self.check("SSE 拒绝匿名读取", self.client.get(path).status_code == 401)
        self.check(
            "SSE 隔离其他账号",
            self.client.get(path, headers={"Authorization": f"Bearer {other}"}).status_code == 404,
        )
        response = self.client.get(path, headers=headers)
        self.check(
            "SSE 真实回放并在终态关闭",
            response.status_code == 200
            and "text/event-stream" in response.headers.get("content-type", "")
            and "event: completed" in response.text,
        )
        ids = [line[4:] for line in response.text.splitlines() if line.startswith("id: ")]
        self.check("SSE 事件不重复", bool(ids) and len(ids) == len(set(ids)))
        replay = self.client.get(path, headers={**headers, "Last-Event-ID": ids[0]})
        self.check(
            "SSE Last-Event-ID 续传",
            replay.status_code == 200
            and f"id: {ids[0]}\n" not in replay.text
            and "event: completed" in replay.text,
        )
        expired = self.client.get(
            path, headers=headers, params={"after_event_id": "ev_expired_synthetic"}
        )
        self.check("SSE 过期游标返回 410", expired.status_code == 410)
        snapshot = self.request("GET", f"/executions/{execution_id}", token=token)
        self.check(
            "SSE 可回退权威快照",
            snapshot["status"] == "succeeded" and bool(snapshot["latest_event_id"]),
        )

    def check_lifecycle(self, context):
        """仅对本次新建的合成资料执行更新和删除。"""
        token = context["token"]
        pe = self.request(
            "POST",
            "/plans",
            token=token,
            expected=202,
            json={
                "analysis_id": context["analysis_id"],
                "deadline": (date.today() + timedelta(days=7)).isoformat(),
                "timezone": "Asia/Shanghai",
                "daily_minutes": 90,
            },
        )["execution_id"]
        draft = self.wait_execution(token, pe)
        plan_id = draft["pending_input"]["plan_id"]
        doc_id = context["document_id"]
        document = self.request("GET", f"/documents/{doc_id}", token=token)
        updated = self.request(
            "PUT",
            f"/documents/{doc_id}/text",
            token=token,
            expected=202,
            json={
                "expected_version": document["current_version"],
                "extracted_text": "合成新版本：负责 MongoDB 集合、字段和索引设计。",
            },
        )
        self.wait_execution(token, updated["execution_id"])
        self.request("POST", f"/plans/{plan_id}/confirm", token=token, expected=409)
        self.check("来源更新后拒绝确认旧草稿", True)
        execution = self.request(
            "POST",
            "/analyses",
            token=token,
            expected=202,
            json={"job_id": context["job_id"], "document_ids": [doc_id]},
        )["execution_id"]
        waiting = self.wait_execution(token, execution)
        self.check("新分析确实进入追问", waiting["status"] == "waiting_for_input")
        pending = waiting["pending_input"]
        self.request("DELETE", f"/documents/{doc_id}", token=token, expected=202)
        self.request(
            "POST",
            f"/executions/{execution}/resume",
            token=token,
            expected=409,
            json={
                "interrupt_id": pending["interrupt_id"],
                "checkpoint_id": pending["checkpoint_id"],
                "answer": {"continue_without_evidence": True},
            },
        )
        self.check("来源删除后拒绝恢复", True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--output", type=Path, default=Path("storage/agent/m2-acceptance.json"))
    parser.add_argument("--include-lifecycle-checks", action="store_true")
    args = parser.parse_args()
    audit = M2Audit(args.base_url, args.timeout)
    try:
        context = audit.run(
            credential("M2_USER_EMAIL", "测试账号"),
            credential("M2_USER_PASSWORD", "测试密码", password=True),
            credential("M2_OTHER_EMAIL", "第二账号"),
            credential("M2_OTHER_PASSWORD", "第二密码", password=True),
        )
        if args.include_lifecycle_checks:
            audit.check_lifecycle(context)
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps({"findings": audit.findings}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        audit.client.close()


if __name__ == "__main__":
    main()
