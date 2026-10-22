# M1 合成开发样例

所有人物、经历、岗位、ID 和响应均为人工组织的合成契约样例，不代表真实用户、招聘信息或实际模型运行结果。样例只用于开发和联调，不能作为真实评测成绩或课程实验数据。

## 样例文件

| 文件 | 用途 |
| --- | --- |
| [resume-demo.md](resume-demo.md) | 示例简历，可按 resume 类型上传 |
| [project-demo.md](project-demo.md) | 示例项目说明，可按 project 类型上传 |
| [jd-backend-intern.txt](jd-backend-intern.txt) | 用户复制到 JD 输入框的岗位文本 |
| [requests.json](requests.json) | JD 创建、分析创建和证据查询的请求描述，不含可用令牌 |
| [analysis-response.json](analysis-response.json) | GET /analyses/{id} 的完整示例响应 |
| [evidence-responses.json](evidence-responses.json) | 按请求路径索引的两份证据响应；不是单个 API 的返回格式 |
| [execution-response.json](execution-response.json) | GET /executions/{id} 的 succeeded 快照样例 |
| [execution-events.txt](execution-events.txt) | SSE 阶段、成功及独立失败场景；先供 A/B 对照，真实 SSE 为 M2 |

仅前三个文件作为用户原始输入。JSON、此 README、事件文件和预期答案不得加入个人经历索引；否则会把答案本身当作检索证据。Markdown 以 UTF-8、LF、1 起始行号定位；上传原文件后使用解析器实际行号。转换为其他格式后不得沿用这里的行号。

M1 还必须验证有文本层 PDF。C 需提供可复现的合成多页 PDF 输入或生成方法（包含空白页与后续页的已知片段），B/A 使用它完成真实上传、索引和引用展示；当前目录尚未附 PDF 样例，不能将现有 Markdown 用例当作 PDF 验收已通过。PDF 必须由 Python 后端重新逐页解析，引用使用实际页码，JSON 的预期答案不写入 PDF 或索引。损坏、需要密码、无文本层及含扫描内容的失败用例另行准备，OCR 不属于本轮。

## 固定 ID 与位置

这些 ID 仅方便样例联调。真实上传、岗位创建和执行 API 必须返回服务端生成的 ID，前端不能在真实请求中写死样例 ID。

| 对象 | 样例 ID | 说明 |
| --- | --- | --- |
| 简历 | doc_demo_resume | resume-demo.md，版本 1 |
| 项目 | doc_demo_project | project-demo.md，版本 1 |
| 岗位 | job_demo_backend | 示例 JD，版本 1 |
| 分析 | analysis_demo_001 | analysis-response.json |
| 执行 | exec_demo_analysis_001 | execution-response.json 与事件流 |
| 数据模型片段 | chunk_demo_db | 项目说明的数据库设计原文 |
| 缓存片段 | chunk_demo_cache | 项目说明的缓存原文 |

## 三种预期匹配

| 要求 ID | 要求 | 预期状态 | 理由 |
| --- | --- | --- | --- |
| req_db | 数据库设计 | supported | 项目资料明确记录了字段、关联和索引设计工作 |
| req_perf | 可测量的后端性能优化 | partial | 记录了 Redis 缓存使用，但缺少测量方法和前后对比 |
| req_docker | Docker 镜像与部署 | not_evidenced | 提交资料没有相关证据；这不等于用户不会 Docker |

分析 JSON 的具体措辞不是模型必须逐字复现的标准答案。验收关注状态是否合理、引用是否支持结论、缺失信息是否如实表述。禁止据此虚构百分比匹配分、延迟改善或录用概率。

## 四位成员如何使用

- A：先读取分析和证据响应开发页面，明确显示“样例模式”；完成后将读取器替换为同契约 API 客户端。
- B：按 04 API 文档实现上传、岗位、执行和结果接口；响应保持单资源 envelope，不直接返回整个 fixture 文件映射。
- C：只将简历与项目说明按个人资料入库，验证三个查询的原文证据；不能搜索到其他用户的数据。
- D：用固定 JD、检索工具和结构化校验完成分析；可在开发样例中使用确定性检索替身验证契约，最终联调必须调用 C 的真实模块。

M1 真实链路使用轮询查询任务状态；SSE 文件描述 M2 目标协议，可提前开发界面但不计入 M1 完成条件。后续计划与面试响应不在本轮样例中，避免把未实现功能混进首轮验收。

## 维护与验收边界

修改原始资料时，同步更新证据 quote、locator 和分析中的引用。负责人另建独立保留的验收资料，不把这套公开开发样例当作独立评测集。首轮开发任务和验收入口见 [M1 开工计划](../dev/08-M1开工计划-kickoff-plan.md)。
