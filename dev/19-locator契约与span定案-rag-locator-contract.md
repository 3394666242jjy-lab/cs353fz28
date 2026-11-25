# 19 locator 契约与 span 定案（成员 C）

状态：v0.1，2026-11。关联 [issue #48](https://github.com/3394666242jjy-lab/cs353fz28/issues/48)，
收口 [dev/17](17-M3切分与知识库基线-m3-rag.md) 第 1 节留下的前置条件：
"M2 `TextLocator` 只有行号，不能准确表示一行中拆出的局部片段；M3 在真正接入业务前，
必须增加来源 span 契约**或**保证返回原完整行。"

## 0 定案

三条，按"现在有没有落地"分开写清楚：

| 决定 | 状态 |
| --- | --- |
| **M2 走"保证返回原完整行"这条选项**：`TextLocator` 语义 = chunk 等于归一化全文的连续整行区间 | ✅ 已成立，升格为契约并加测试 |
| **M3 结构化切分不得复用行号接口**：行内切分一律带字符 span，行号只作展示辅助 | ✅ 规则已冻结，测试锁定 |
| **给 `TextLocator` 增加可选的 `char_start`/`char_end`**，文本入库时写入 | ✅ 已落地（第 4 节），OpenAPI 与前端类型同步生成 |

三条现在都落地了，`dev/17` 留下的前置条件就此关闭。旧数据的 locator 只有行号，仍然合法；
新索引的文本 chunk 带字符区间，消费方按"有 span 用 span、无 span 退回行号"处理。

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
| 7 | `char_span()` 的换算覆盖所有行区间；越界显式报错、不夹取 | `test_char_span_equals_the_line_range_join_for_every_pair` |
| 8 | `TextLocator` 的字符区间要么整对出现、要么都不出现；半对与空区间拒绝 | `test_text_locator_accepts_absent_or_complete_span_only` |
| 9 | 文本入库写入字符区间，每个区间能精确切出该 chunk 的原文 | `test_indexer.py::test_text_chunks_are_indexed_with_exact_char_spans` |
| 10 | 证据接口原样透出 span；历史数据如实回 `null`，不凭空补偏移 | `test_api.py::test_evidence_passes_through_char_span_when_present` |

第 1 条里"能换算成精确字符区间"原先不对外输出，它是 span 契约的语义基准；第 7–10 条是
字段落地后补的：换算、校验、入库自检与接口透出各有一条。第 6 条是 dev/17 那句话的执行
版本——一旦有人给结构化切分只填行号、或声称行号够用，这条测试先红。

## 3 PDF 的定位语义

- 现状：PDF 的行号是**页内**行号，页码由 `PdfLocator(page=...)` 另行携带，两者在
  `app/rag/indexer.py` 合并成 locator；空白页不产 chunk，但后续页码不重排。
- 因此 span 若将来用于 PDF，语义必须是**页内偏移**，不能是全文偏移：全文偏移要跨越
  空白页记账，一旦解析器改动（例如 PR #47 的 OCR 版本）就会整体漂移，而页内偏移不受
  影响。
- 本期只对 `TextLocator` 谈 span，`PdfLocator` 保持 page-only。

## 4 span 字段（已落地）

`app/schemas/analysis.py::TextLocator` 增加了两个**可选**字段：

```python
char_start: int | None = Field(default=None, ge=0)
char_end: int | None = Field(default=None, ge=1)
```

| 项 | 定义 |
| --- | --- |
| 语义 | 相对 `ParsedDocument.text`（LF 归一化后全文）的 0 起始半开区间 `[char_start, char_end)` |
| 不变式 | `text[char_start:char_end] == quote`；两个字段**同时存在或同时缺失**；`char_end > char_start` |
| 缺失时 | locator 退化为"M2 语义"：只表示整行区间。消费方必须支持两种形态 |
| 语义版本 | 无 span 记 `text-line-v1`；带 span 记 `text-span-v1`（见下条说明） |
| 消费方规则 | 有 span 用 span 精确定位；无 span 退回行号。**不允许**把 span 塞进行号字段，也不允许按比例换算 |

配套实现：

- `app/rag/chunking.py::char_span(lines, line_start, line_end)` 负责换算，行区间越界直接
  报错、不夹取。切分阶段（`chunk_document`）仍然只给行号——**字符区间在入库时才加**，
  因为"行号 vs 页内行号"的语义只在入库时才知道；
- `app/rag/indexer.py::index_document` 为文本 chunk 写入 span，并在写库前自检
  `parsed.text[start:end] == chunk.text`，不一致就拒绝入库，绝不写入偏移错位的定位；
- PDF 仍走 `PdfLocator(page=...)`，不带 span（第 3 节）。

为什么不是"把行号换成分数行"（如 `line_start=12.4`）：那会让本来离散可校验的行号变成
浮点数，既不能还原原文，也无法和已存数据比较。

**关于 `text-span-v1`**：这个标记目前只写在文档里，没有进 `index_version`。原因是不想
让既有索引元数据的格式无谓变动；实际效果是"老索引只有行号、新索引带 span"，而 span 是
可选字段，两种形态都能被同一份契约校验。若后续需要在索引维度上区分，再单独加即可。

## 5 落地时的前置与已解除情况

| 前置 | 状态 |
| --- | --- |
| CI 强制同步生成物（`export_openapi.py` + `npm run api:types` + `git diff --exit-code`） | ✅ 已执行 `npm ci`，两个生成物重新生成，diff 仅包含 `TextLocator` 的两个新字段 |
| 公共契约变更需负责人确认 | ⏸ 提交 PR 时按 `AGENTS.md` 在描述里单列 |
| 消费方按行号工作（B 的 `chunks.locator`、D 的 `Evidence.locator`、A 的两个页面、`examples/`、dev/03、dev/04） | ✅ 加可选字段不影响运行；本轮只更新了 dev/03 与 dev/04 的契约描述 |

部署注意事项：`ContractModel` 是 `extra="forbid"`，所以**旧代码读新数据会报错**。升级顺序
应为"先升后端与消费方，再让带 span 的数据落地"；同一套部署内不存在混跑问题。

## 6 与 M3 上线的关系

结构切分（`app/rag/structured_chunking.py`）已经在产出精确的 `char_start`/`char_end`
与行号，并且它的 `text` 是 `text[start:end]` 的精确切片。所以 M3 接入时走方案甲：

- **方案甲（已选）**：span 字段已落地，结构化切分直接带上 span；
- 方案乙（把结构切分改成按行边界对齐、放弃行内切）不再需要。

无论走哪个，都不允许第三种做法：只填行号、让引用位置看起来对其实差半行。

## 7 复跑

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests/test_locator_contract.py tests/test_chunking.py tests/test_indexer.py -q
.\.venv\Scripts\python.exe -m ruff check --config pyproject.toml app tests ../scripts

cd ..
backend\.venv\Scripts\python.exe scripts/export_openapi.py
npm run api:types --prefix frontend
git diff --exit-code -- frontend/src/api/openapi.json frontend/src/api/schema.d.ts
```

最后一行是 CI 里的同步检查，本地应留下"已生成、无差异"的状态。

## 8 边界

- span 只加在 `TextLocator` 上：PDF 的字符级定位（页内偏移）仍属后续工作，需要先与
  PR #47 的解析器版本对齐；
- `dev/17` 是负责人名下的基线记录，不去改它，而是在这里把它的前置条件收口为
  "M2 已满足 / M3 规则已定 / 字段已落地"；
- 前端目前只显示"第 X–Y 行"，尚未使用 span 做逐字高亮——那是 A 的界面工作，本轮只保证
  契约与接口能把它送出去；
- 检索与评测相关的变化见 [dev/13](13-M2检索策略与评测-rag-m2-spec.md)、
  [dev/18](18-证据充分性评测-rag-support-eval.md)。
