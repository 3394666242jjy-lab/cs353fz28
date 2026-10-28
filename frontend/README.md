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

- 后端业务接口仍返回 501，真实上传/分析需等 B #7 合并；
- 登录接口未实现，`Authorization` 头为占位（`setAuthToken`）；
- 证据的 PDF 页码展示已按 `locator.kind` 实现，尚未用真实 PDF 联调；
- 依赖 C #8 的解析与检索结果。

## 自查记录

- 2026-10-27：`tsc --noEmit` 通过；`vite build` 通过（28 modules）；`prettier --check src` 通过。
- 已知问题：真实联调依赖 B/C 合并，见上。
