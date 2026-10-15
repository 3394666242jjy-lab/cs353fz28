# CareerPilot Git 协作规范

本规范适用于五位成员及自动化助手提交的代码、文档、配置和数据库变更。核心流程是：**Issue → 工作分支 → 规范 Commit → 关联 Issue 的 PR → 他人审查 → 合并 main**。

## 分支规则

- `main` 为稳定主分支。禁止直接在 main 开发、提交或推送，禁止强制推送 main。
- 每个工作分支从最新 main 创建，围绕一个可审查任务；不创建多人长期共用的个人分支。
- `dev/` 是开发文档目录；当前不设同名集成分支，PR 的目标为 main。
- 团队分支格式为 `<type>/<issue>-<short-description>`；自动化助手使用 `codex/<issue>-<short-description>`。

| 类型 | 用途 | 示例（编号仅为示例） |
| --- | --- | --- |
| feat | 新功能 | feat/42-jd-import |
| fix | 问题修复 | fix/43-evidence-owner-check |
| docs | 文档 | docs/44-api-spec |
| refactor | 不改变行为的重构 | refactor/45-retrieval-adapter |
| chore | 依赖、构建和维护 | chore/46-python-tooling |
| codex | Codex 执行的任务 | codex/47-project-docs |

## Issue 规范

编码或改文档前创建或认领 Issue，避免重复工作。Issue 至少包含：

1. 问题或目标，以及涉及的用户故事。
2. 本次范围及不包含的内容。
3. 可检查的验收条件。
4. 负责人、依赖及相关文档。

每个 Issue 指定一位主要负责人；多人协作可拆子任务。没有确认 GitHub 账号时不得猜测指派。范围变更及时更新 Issue。一个 PR 可以完成多个紧密相关的 Issue，不把无关改动拼在一起。

## Commit 格式

使用 Conventional Commits 风格的团队约定：

```text
<type>(<scope>): <简明描述>

<可选正文：为什么修改、行为变化或兼容性>

Refs #<Issue编号>
```

| type | 说明 |
| --- | --- |
| feat | 新增用户可见能力 |
| fix | 修复错误 |
| docs | 文档变化 |
| refactor | 不改变外部行为的代码重构 |
| test | 检查或测试代码变化 |
| build | 构建系统或依赖变化 |
| ci | 持续集成配置 |
| chore | 其他维护 |
| perf | 性能优化 |
| revert | 回退既有变更 |

scope 推荐使用 frontend、backend、rag、agent、db、api、dev 或 repo。主题使用祈使或明确动作，简短说明实际变化；不得只写 update、fix bug、修改或最终版。破坏兼容性的改动使用 `type(scope)!:`，正文写 `BREAKING CHANGE:` 并说明迁移方式。

正确示例（Issue 编号仅为示例）：

```text
feat(rag): add document version filtering

Prevent obsolete resume chunks from appearing in retrieval results.

Refs #42
```

一次 Commit 解决一个清楚的问题，避免把格式化全库和业务修改混在一起。提交前检查暂存区，只加入本次相关文件。不得用别人的身份提交，不通过刷无意义 Commit 数量冒充贡献。

## 本地操作流程

以下以示例 Issue #42 为例；仅在远程 main 已存在时执行。

```bash
git switch main
git pull --ff-only origin main
git switch -c feat/42-jd-import

# 完成修改和模块自查后，先检查差异
git status --short
git diff

# 逐项添加本次任务文件；不盲目 git add .
git add <本次文件路径>
git diff --cached
git diff --cached --check
git commit -m "feat(backend): add JD import endpoint" -m "Refs #42"
git push -u origin feat/42-jd-import
```

推送前确认 `git branch --show-current` 不是 main。中文或含空格路径使用引号。未暂存或未跟踪的个人文件不得随任务误提交。

## PR 规范

- 工作分支推送后创建 PR，base 为 main，head 为本次工作分支。
- PR 标题与 Commit 采用相同类型格式，准确描述最终改动。
- 描述必须说明问题、结果、范围、验证方式和关联 Issue；界面变化附适当截图，数据或接口变更说明兼容性。
- 完整解决 Issue 使用 `Closes #42`；仅部分进展使用 `Refs #42`，不能提前关闭尚未满足验收的 Issue。
- 尚未可审查的工作使用 Draft PR；完成后再标记 Ready for review。
- 使用仓库 PR 模板，AI 辅助内容说明工具、用途和核验方式。

PR 描述示例：

```markdown
## 变更

新增 JD 导入与输入校验，用户可以保存岗位原文并查看岗位记录。

## 验证

- 实际执行的命令或手工验证步骤及结果。
- 尚未执行的检查与原因。

## 关联 Issue

Closes #42

## 兼容性与 AI 辅助

说明接口／Schema／配置变化；记录 AI 工具、用途及核验方式，没有则写无。
```

## 审查与合并

1. 至少一位其他成员审查；作者不能批准自己的 PR。
2. 涉及数据库、鉴权、公共 API 或架构的改动，由负责人额外确认。
3. 修复审查意见，解决所有阻塞讨论，完成适用检查；未配置 CI 时记录手工检查，不能声称 CI 已通过。
4. 负责人或约定维护者执行 Squash and merge，整理最终 Commit 为规范格式，保留 Issue 关联与实际作者署名。
5. 禁止未审查直接合并、绕过检查、自动合并自己的未审查 PR 或强推 main。
6. 合并后确认 Issue 状态，再删除已合并工作分支；不删除他人仍在使用的分支。

## 同步与冲突

工作中定期同步 main。共享分支优先使用 merge 保留协作历史；仅个人独占分支可在与协作者确认后 rebase。确需重写已推送的个人分支时使用 `--force-with-lease`，禁止普通 `--force`，且绝不对 main 使用。冲突解决后重新执行受影响的检查，不能直接选择 ours/theirs 丢弃他人改动。

## 禁止提交的内容

真实简历及面试回答、密钥、令牌、`.env`、数据库备份、上传文件、日志、虚拟环境、node_modules、构建产物和临时文件。`.env.example` 仅能包含变量名和占位值。发现凭据泄露时立即停止传播并通知负责人，先撤销或轮换凭据；单纯删除最新文件不能清除历史泄露。

## 空仓库首次初始化

没有任何提交的远程仓库无法形成工作分支到 main 的可比较 PR。首次需由仓库所有者明确建立一个最小 main 基线；这是仓库初始化操作，不属于日常功能提交。此后所有项目改动均走工作分支和 PR，不得以初始化为由直接提交完整项目到 main。

## GitHub 保护设置建议

所有者可为 main 配置必须走 PR、至少一次审批、解决审查讨论及禁止强推／删除；CI 配置完成后再启用相应必需状态检查。**本文只定义规范，不代表远程保护规则已经启用。** 能力和可用设置以仓库实际支持为准。
