# AGENTS.md — 提交与协作规则（供 AI 助手遵守）

本仓库遵循 [`dev/07-Git协作规范-git-workflow.md`](dev/07-Git协作规范-git-workflow.md)。AI 助手在提交前必须执行以下检查。

## 提交前检查

1. **先确认关联 Issue 已创建或认领**，再开分支、写代码。
2. **拉取最新 main 并检查冲突**（禁止在未同步的情况下直接推送）：
   ```bash
   git fetch origin main
   git merge origin/main --no-commit --no-ff
   # 或 git rebase origin/main
   ```
   有冲突必须先解决并重新跑检查。
3. **只暂存本次相关文件**，不要 `git add .`；提交前用 `git status --short` 和 `git diff --cached` 核对。
4. **运行本次模块适用的检查**（按实际改动选择）：
   ```bash
   cd frontend && npm run typecheck && npm run build && npm run format:check
   cd backend && uv run pytest
   ```

## Commit 粒度：一个模块一个 commit

- 每个 commit 只做一件事，按模块拆分（例如：API client、上传、分析、接线、文档）。
- 格式：`<type>(<scope>): <简明描述>`，正文用 `Refs #<Issue编号>` 关联任务。
- 不把无关改动、格式化全库与业务修改混在同一个 commit。

## PR 规则

- base 为 `main`；**一个 PR 可以关联多个 Issue**。
- 完整解决用 `Closes #N`；仅部分完成用 `Refs #N`，不得提前关闭未满足验收的 Issue。
- 描述必须包含：变更、验证命令与结果、关联 Issue、兼容性与 AI 辅助说明。
- 至少一位其他成员审查；作者不得批准自己的 PR。
