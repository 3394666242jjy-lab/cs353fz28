# 19 locator 契约与 span 定案（成员 C）

状态：v0.1，2026-11。关联 [issue #48](https://github.com/3394666242jjy-lab/cs353fz28/issues/48)，
收口 [dev/17](17-M3切分与知识库基线-m3-rag.md) 第 1 节留下的前置条件：
"M2 `TextLocator` 只有行号，不能准确表示一行中拆出的局部片段；M3 在真正接入业务前，
必须增加来源 span 契约**或**保证返回原完整行。"

## 0 定案

三条，按"现在有没有落地"分开写清楚：

| 决定 | 状态 |
| --- | --- |
| **M2 走"保证返回原完整行"这条选项**：`TextLocator` 语义 = chunk 等于归一化全文的连续整行区间 | ✅ 已成立，本轮升格为契约并加测试 |
| **M3 结构化切分不得复用行号接口**：行内切分一律带字符 span，行号只作展示辅助 | ✅ 规则已冻结，测试锁定 |
| **给 `TextLocator` 增加 `char_start`/`char_end` 字段** | ⏸ 未落地，字段级提案见第 4 节 |

也就是说：**技术债里"C 侧可控"的一半已经清掉**——M2 的引用诚实有了明文契约与回归
测试，M3 的越界用法被提前堵死；剩下的是"加字段"这一步，它卡在公共契约与生成物上，
不是设计问题。

## 1 事实依据

M2 的切分器本来就满足"整行"这一条，只是这件事此前只存在于实现细节里：

- `backend/app/rag/chunking.py` 模块文档（第 6–12 行）写明两条不变式：chunk 是连续行
  区间（`"\n".join(lines[start-1:end]) == chunk.text`），且不在段落中间强切；
- `_blocks()` 不拆超长段落，`_trim_unit()` 只裁首尾纯空行，`_make_chunk()` 用
  `"\n".join(lines[start-1:end])` 拼 quote；
- `backend/tests/test_chunking.py:45` 的 `test_chunks_are_verbatim_contiguous_line_ranges`
  已经断言逐字还原、不重叠、非空行全覆盖。

所以本轮不做新功能，做的是**把既有行为写成契约**：条款有名字、有测试、有失败时的解释。

## 2 契约条款（由 `backend/tests/test_locator_contract.py` 锁定）

| # | 条款 | 测试 |
| --- | --- | --- |
| 1 | `quote` 逐字等于 locator 行区间，**并且**能换算成精确字符区间 | `test_text_quote_equals_its_line_range_and_char_span` |
| 2 | 行区间不重叠；每个非空行恰好被一个 chunk 覆盖 | `test_line_ranges_are_disjoint_and_cover_every_non_blank_line` |
| 3 | PDF 逐页切分、chunk 不跨页；空白页不产 chunk 也不影响后一页 | `test_pdf_pages_are_chunked_independently_and_blank_page_yields_nothing` |
| 4 | 行号针对 LF 归一化后的文本，`quote` 不含回车符 | `test_crlf_source_is_chunked_against_normalized_text` |
| 5 | 区间内的空行属于 `quote`，不压缩不删除 | `test_blank_lines_inside_the_range_stay_verbatim` |
| 6 | M3 行内切分**不能**用行号表达，必须带字符 span | `test_structured_chunk_split_inside_a_line_cannot_reuse_line_numbers` |

第 1 条里"能换算成精确字符区间"现在不对外输出，但它正是 span 契约的语义基准：**先证明
M2 输出天然满足它，将来加字段时不需要改切分器**。第 6 条是 dev/17 那句话的执行版本——
一旦有人给结构化切分只填行号、或声称行号够用，这条测试先红。

## 3 PDF 的定位语义

- 现状：PDF 的行号是**页内**行号，页码由 `PdfLocator(page=...)` 另行携带，两者在
  `app/rag/indexer.py` 合并成 locator；空白页不产 chunk，但后续页码不重排。
- 因此 span 若将来用于 PDF，语义必须是**页内偏移**，不能是全文偏移：全文偏移要跨越
  空白页记账，一旦解析器改动（例如 PR #47 的 OCR 版本）就会整体漂移，而页内偏移不受
  影响。
- 本期只对 `TextLocator` 谈 span，`PdfLocator` 保持 page-only。

## 4 span 字段提案（未落地）

给 `app/schemas/analysis.py::TextLocator` 增加两个**可选**字段：

```python
char_start: int | None = Field(default=None, ge=0)
char_end: int | None = Field(default=None, ge=1)
```

| 项 | 定义 |
| --- | --- |
| 语义 | 相对 `ParsedDocument.text`（LF 归一化后全文）的 0 起始半开区间 `[char_start, char_end)` |
| 不变式 | `text[char_start:char_end] == quote`；两个字段**同时存在或同时缺失**；`char_end > char_start` |
| 缺失时 | locator 退化为"M2 语义"：只表示整行区间。消费方必须支持两种形态 |
| 语义版本 | 无 span 记 `text-line-v1`；带 span 记 `text-span-v1`，并进入切分/索引版本号留档 |
| 消费方规则 | 有 span 用 span 精确定位；无 span 退回行号。**不允许**把 span 塞进行号字段，也不允许按比例换算 |

为什么不是"把行号换成分数行"（如 `line_start=12.4`）：那会让本来离散可校验的行号变成
浮点数，既不能还原原文，也无法和已存数据比较。

## 5 为什么本期不加字段

不是设计未定，而是三个具体前置：

1. **CI 有强制同步检查**。`.github/workflows/check.yml` 的 frontend job 会执行
   `export_openapi.py` + `npm run api:types` 并 `git diff --exit-code`；改 `TextLocator`
   必须同时重生成 `frontend/src/api/openapi.json` 与 `frontend/src/api/schema.d.ts`。
   前者用 python 可得，后者需要 `openapi-typescript`，而本机 `frontend/node_modules`
   不存在，要先 `npm ci`。
2. **这是公共契约变更**。按 `AGENTS.md`，公共接口/数据结构变化要在 PR 里单独说明并由
   负责人确认；加可选字段对已存数据向后兼容，但**旧代码读新数据**会因
   `ContractModel` 的 `extra="forbid"` 直接报错，属于部署顺序问题，不能默默上线。
3. **消费方要一起确认**。当前按 `line_start`/`line_end` 工作的有：B 的
   `documents.py` 存储与 `chunks.locator`、D 的 `Evidence.locator`、A 的
   `AnalysisPanel.tsx` / `JdImport.tsx` 展示（"第 X–Y 行"）、`examples/evidence-responses.json`
   示例、以及 `dev/03`、`dev/04` 的契约描述。加字段不影响它们运行，但需要同步说明。

## 6 与 M3 上线的关系

结构切分（`app/rag/structured_chunking.py`）已经在产出精确的 `char_start`/`char_end`
与行号，并且它的 `text` 是 `text[start:end]` 的精确切片。所以 M3 接入时可以二选一：

- **方案甲（推荐）**：先落地第 4 节的 span 字段，结构化切分直接带上 span；
- **方案乙**：把结构切分改成按行边界对齐（放弃行内切），代价是长段落会超出目标尺寸。

无论选哪个，都不允许第三种做法：只填行号、让引用位置看起来对其实差半行。

## 7 复跑

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests/test_locator_contract.py tests/test_chunking.py -q
.\.venv\Scripts\python.exe -m ruff check --config pyproject.toml app tests ../scripts
```

## 8 边界

- 本轮只动 C 的测试与文档，未改 schema、未改切分器、未动 `openapi.json`/`schema.d.ts`，
  因此对 B/D/A 零影响；
- `dev/17` 是负责人名下的基线记录，本轮不去改它，而是在这里把它的前置条件收口到
  "M2 已满足 / M3 规则已定 / 字段待落地"三行；
- 检索与评测相关的变化见 [dev/13](13-M2检索策略与评测-rag-m2-spec.md)、
  [dev/18](18-证据充分性评测-rag-support-eval.md)。
