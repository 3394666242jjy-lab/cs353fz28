# CareerPilot 前端模块（A 前端）

React + TypeScript + Vite 实现 M1 的页面与轮询交互：资料上传、JD 导入、带证据的分析展示。

## 启动

```bash
cd frontend
npm ci            # 或 npm install
npm run dev       # http://127.0.0.1:5173
```

后端默认 `http://127.0.0.1:8000`；地址不同时设置 `VITE_API_BASE_URL`。

## 脚本

| 命令 | 作用 |
| --- | --- |
| `npm run dev` | 本地开发 |
| `npm run build` | 类型检查 + 生产构建 |
| `npm run typecheck` | 仅类型检查 |
| `npm run format:check` | 格式检查 |

## 目录

- `src/App.tsx`：三步工作台（资料准备 → 目标岗位 → 匹配分析）
- `src/pages/DocumentUpload.tsx`：资料上传 + 索引状态轮询
- `src/pages/JdImport.tsx`：JD 分析 + 执行状态轮询 + 证据展示
- `src/pages/AnalysisPanel.tsx`：样例分析展示
- `src/api/client.ts`：集中 API 客户端
- `src/api/errors.ts`：统一错误描述
- `src/api/schema.d.ts`：由 OpenAPI 生成

## 接口映射

| 功能 | 调用 |
| --- | --- |
| 上传资料 | `POST /api/v1/documents`（multipart） |
| 资料状态 | `GET /api/v1/documents/{id}`（轮询） |
| 创建岗位 | `POST /api/v1/jobs` |
| 创建分析 | `POST /api/v1/analyses` |
| 执行状态 | `GET /api/v1/executions/{id}`（轮询，终态停止） |
| 分析结果 | `GET /api/v1/analyses/{id}` |
| 原文证据 | `GET /api/v1/evidence/{chunk_id}?version=N` |

## 样例模式与真实模式

- 「样例演练」不调用后端，本地演示轮询与三种结论；
- 「真实分析」调用后端，需要至少一份已就绪资料；刷新后通过 `sessionStorage` 恢复轮询。

## 当前限制（待联调）

- 本地登录使用种子账号，登录令牌仅存于当前浏览器会话；退出或切换账号会清理会话资料。
- 真实上传及分析需要 MongoDB、Redis、Qdrant、Embedding 与生成模型可用。
- PDF 只支持可提取文本的文件；扫描件 OCR 尚未实现。
- 部署环境仍需执行真实依赖和模型的端到端验收。

## 自查记录

- 2026-10-27：`tsc --noEmit` 通过；`vite build` 通过（28 modules）；`prettier --check src` 通过。
- 本地服务与真实模型的联调状态见上。
