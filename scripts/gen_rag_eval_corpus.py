"""生成 M2 检索评测的合成语料与问题集（成员 C，issue #25）。

产物写入 ``backend/tests/data/rag_eval/``：

* ``corpus/`` —— 3 个用户 × 5 个文档，含多版本与多页 PDF（第二页留空，用于页码定位）；
* ``questions.jsonl`` —— 60 个问题，36 个可回答 + 24 个负例，标注指向真实 chunk_id。

设计约束（见 dev/13 与同目录 README）：

* 语料全部合成，禁止真实简历；内容可公开评审；
* 必须同时存在"只有字面匹配才命中"的事实（版本号、毫秒数、专有名词）与
  只能用语义命中的事实，否则 dense 与 hybrid 的差异无从体现；
* 标注的 chunk_id 必须与运行期 ``index_document`` 完全一致——本脚本直接调用
  ``parse_document`` + ``chunk_document`` 按冻结的 CHUNK_MAX_CHARS 切分，再用
  ``make_chunk_id`` 派生，因此标注不会与索引结果漂移。

用法：

    python scripts/gen_rag_eval_corpus.py            # 生成并校验
    python scripts/gen_rag_eval_corpus.py --check    # 只校验现有产物是否可复现
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"


def rag_modules():
    """延迟导入后端 RAG 模块。

    脚本位于仓库根、后端包在 ``backend/`` 下，因此运行期才把该目录加入 sys.path；
    放在函数内可避免"import 之前先执行语句"的告警。
    """
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from app.rag.chunking import chunk_document, parse_document
    from app.rag.indexer import make_chunk_id

    return parse_document, chunk_document, make_chunk_id


# 与冻结基线一致：生成标注时用同一参数切分，否则 chunk_id 会与索引结果不一致。
CHUNK_MAX_CHARS = 500

DATA_DIR = ROOT / "backend" / "tests" / "data" / "rag_eval"
CORPUS_DIR = DATA_DIR / "corpus"
QUESTIONS_PATH = DATA_DIR / "questions.jsonl"

# 自证性事实：答案只存在于某一版本的某一个 chunk 里，便于人工复核标注。
# 第二项是 index_document 实际接收的 file_type——B 的 worker 传的是文件后缀。
DOC_META = {
    "resume": ("resume.md", ".md"),
    "project_a": ("project_a.md", ".md"),
    "project_b": ("project_b.md", ".md"),
    "notes": ("interview_notes.txt", ".txt"),
    "cert": ("certificate.pdf", ".pdf"),
}

USERS = {
    "alpha": {
        "user_id": "user_eval_alpha",
        "role": "Python 后端工程师",
        "stack": "FastAPI",
        "db": "MySQL",
        "cache": "Redis",
    },
    "beta": {
        "user_id": "user_eval_beta",
        "role": "Java 后端工程师",
        "stack": "Spring Boot",
        "db": "PostgreSQL",
        "cache": "Kafka",
    },
    "gamma": {
        "user_id": "user_eval_gamma",
        "role": "前端工程师",
        "stack": "React",
        "db": "IndexedDB",
        "cache": "Vite",
    },
}


@dataclass(frozen=True)
class Fact:
    """一个可回答问题：marker 必须在正文里逐字出现，query 用于检索。"""

    doc: str
    version: int
    marker: str
    query: str
    style: str  # "literal" 含字面 token，"paraphrase" 只能语义命中


@dataclass(frozen=True)
class Negative:
    """一个负例。

    期望分两种语义，不能混为一谈：

    * ``expect="empty"``：检索结果必须为空（无证据、跨用户、已删除）；
    * ``expect="forbidden_absent"``：允许有其他结果，但被取代的旧版本 chunk 绝不能出现
      （旧版本过滤）。用"结果必须为空"去要求它会误判——语义相近的新版本 chunk
      本就应该被召回。
    """

    kind: str  # no_evidence | cross_user | old_version | deleted
    query: str
    note: str
    expect: str = "empty"
    forbidden_marker: str | None = None
    forbidden_doc: str | None = None
    forbidden_version: int = 1
    # 跨用户负例的禁用 chunk 属于另一个用户，需按那个用户的语料解析。
    forbidden_user: str | None = None
    # 删除负例禁用的是整份文档的全部 chunk，而不是某一个。
    forbid_all: bool = False


def resume_text(short: str, user: dict, extra: dict) -> str:
    """简历正文：三条事实分散在不同小节，且篇幅足以切出多个 chunk。"""
    return f"""# 简历 - {user["role"]}（{short}）

## 基本信息

- 目标岗位：{user["role"]}，偏好业务复杂度高、需要长期打磨稳定性的方向
- 主要技术栈：{user["stack"]}、{user["db"]}、{user["cache"]}
- 工作年限：五年，其中三年在做交易与订单方向，两年在做内部工具与数据看板
- 期望地点：长三角，接受混合办公；到岗时间一个月内

## 工作经历

第一段经历：在一家电商平台负责订单履约链路，参与从单体到服务拆分的迁移。
迁移期间最麻烦的不是拆服务本身，而是历史数据的兼容与灰度切换的编排。期间主导
了幂等与重试方案，把重复下单问题从偶发投诉压到可以观测和归因的范围内。日常还
需要与客服、风控两个团队对齐口径，处理对账差异，很多时间花在把术语说到一致上。

第二段经历：转入中台团队做基础能力，负责配置中心与监控聚合两块。这一阶段的
收获是学会了先定义"正常"再谈告警，把大量噪声规则下掉，让值班同学真正愿意看
告警。同时推进了慢查询治理，按业务域分摊指标，让优化工作可以被持续追踪。

## 典型问题复盘

一次大促前的容量评估里，团队原计划按历史峰值乘以固定系数扩容。我在复盘中发现
流量结构已经变化，短链路的请求占比上升，最终改为按依赖分别估容量，避免了对数据
库的过度预留。这件事让我更相信"先量化再决策"，而不是沿用上一次的经验系数。

## 生产环境版本

- 服务框架：{extra["stack_version"]}

发布与回滚都按版本为单位，镜像构建后先在灰度地域跑一轮真实流量，确认没有异常
指标再逐步放量。灰度期间关注的是错误率与延迟分位，而不是均值——均值很容易被长
尾掩盖，出问题时往往先在某些分位上体现。回滚策略保持在十分钟内可完成，且回滚
不需要改配置，只切版本号，避免故障期还要做额外判断。

值班手册里明确了三类告警的处置顺序，先止损再定位，避免一边查一边扩大影响面。
每次故障复盘都会落到文档，写明时间线、影响面与后续动作，动作要能被追踪到人。

## 数据库与容量

- 数据库：{extra["db_version"]}
- 缓存的容量按大促峰值预留，平时保持三成余量，避免临时扩容来不及

容量评估习惯按依赖分别做，而不是只看整体 QPS：同一个请求可能拖着多个依赖，瓶颈
往往集中在最慢的那一环。压测前会先确认数据量级与分布，否则得到的结论无法外推到
生产。慢查询治理按业务域分摊，谁引入谁负责优化，指标进日报便于持续跟踪。

## 职责概述

- 负责订单服务的接口设计与稳定性治理，长期跟进线上故障复盘。
- 与数据团队协作维护日报与指标口径，保证上下游对账一致。
- 参与新人培养，负责 onboarding 文档与代码评审规范。
- 承担跨团队的方案评审，需要在收益与工期之间给出可执行的取舍建议。

## 协作与沟通

习惯把决策写成短文档而不是只在会议里口头对齐，尤其涉及跨团队接口时。评审前会
先把边界条件与失败路径列清楚，减少实现期的返工。与产品沟通时倾向于先确认验收
标准，再讨论具体方案。

## 教育背景

计算机相关专业本科，课程方向偏系统与数据库，毕业设计做的是查询优化相关题目。

## 技能自评

- 熟练：接口设计、缓存治理、线上问题定位
- 一般：容量规划、跨团队协作推动
- 待补：分布式事务的工程化实践
"""


def project_a_v1(short: str, user: dict, extra: dict) -> str:
    """项目 A 的 v1：两条事实，专门用于旧版本过滤负例。"""
    return f"""# 项目 A 阶段汇报（{short} 初版）

## 背景

订单服务在促销期间出现明显抖动，接口超时率上升，下游依赖被拖慢。团队决定先
定位瓶颈再谈优化，避免在没有数据支撑的情况下盲目扩容。初版汇报只覆盖第一轮
压测的数据，后续还需要补充分片与预热方案的验证结果，因此结论带有阶段性。

## 观测手段

补充了链路追踪与慢查询日志，按小时聚合关键指标，并对数据库连接池做了水位监控。
监控面板同时暴露给业务方，减少口径争议。为了区分"应用慢"和"数据库慢"，在
关键的几个接口上打了分段耗时，把等待时间拆到具体依赖上。

## 第一轮压测设置

压测在预发环境进行，数据量约为生产的六成，流量模型按大促峰值的比例缩放。为了
避免压测本身污染观测，预热阶段的数据单独标注，不计入结论。并发梯度按每分钟一
档逐步抬升，每档稳定运行十分钟后再记录指标。

## 阶段结论

- 上线缓存后，缓存命中率 60%，仍有大量请求穿透到数据库。
- 压测结果显示每秒约 300 次请求，距离目标吞吐有较大差距。
- 结论：瓶颈主要在缓存命中率与数据库连接复用，而非应用层计算。

## 风险与未决项

缓存键的设计还没有经过热点验证，长尾键可能会在真实流量下表现不同。另外连接池
上限的调整需要与数据库团队确认，单方面放大可能把压力转移给数据库。

## 后续计划

下一阶段计划调整分片策略并补齐监控，同时评估读写分离的收益。还需要安排一次
针对热点键的专项验证，确认打散方案是否真的能降低单分片压力。
"""


def project_a_v2(short: str, user: dict, extra: dict) -> str:
    """项目 A 的 v2：事实被改写，旧版本结论不再成立。"""
    return f"""# 项目 A 阶段汇报（{short} 修订版）

## 背景

订单服务在促销期间出现明显抖动，接口超时率上升，下游依赖被拖慢。团队决定先
定位瓶颈再谈优化，避免在没有数据支撑的情况下盲目扩容。修订版基于第二轮压测，
覆盖了分片与预热方案上线后的结果，取代初版的阶段性判断。

## 观测手段

补充了链路追踪与慢查询日志，按小时聚合关键指标，并对数据库连接池做了水位监控。
监控面板同时暴露给业务方，减少口径争议。第二轮压测额外增加了缓存命中率的分层
统计，区分热点键与长尾键，便于判断优化到底作用在哪一层。

## 第二轮压测设置

在与初版相同的预发环境与流量模型下重跑，唯一的变量是分片策略与预热流程。为
保证可比性，压测脚本、并发梯度与记录窗口都沿用第一轮的定义，只在结论部分替换
数据。两轮结果的差异因此可以归因到方案本身，而不是环境波动。

## 阶段结论（命中率）

- 调整分片与预热策略后，缓存命中率 92%，穿透流量显著下降。

命中率提升主要来自预热流程：在大促前按访问排序把热点数据提前灌入，使请求在流量
起来之前就已经命中。键打散是次要贡献，它解决的是单分片过载，而不是命中率本身。
长尾键的命中率仍低于预期，继续优化的边际收益不高，因此本轮不再投入。

## 阶段结论（吞吐）

- 压测结果显示每秒约 1200 次请求，达到目标吞吐。

吞吐提升的归因比较清楚：连接池复用与分片并行共同起作用，其中分片并行贡献更大。
在压测的并发梯度上，吞吐在某个节点之后不再随并发线性增长，说明已经触到下游的
处理上限，继续加压只会把延迟抬高，这一点需要写进后续的容量规划结论。

## 风险与未决项

预热流程依赖一份热点键清单，清单的维护成本还不低，需要评估能否自动化。此外
长尾键的命中率仍低于预期，是否继续优化要看投入产出比。

## 与初版的差异

初版汇报中的命中率与吞吐数据已被本轮压测取代，对外只引用修订版的结论；初版
保留在版本历史中，仅用于追溯当时的判断依据，不再作为决策输入。
"""


def project_b_text(short: str, user: dict, extra: dict) -> str:
    """项目 B：一条字面事实（向量库与维度）+ 一条只能语义命中的效果描述。"""
    return f"""# 项目 B 技术方案（{short}）

## 需求

需要在个人资料范围内做问答，回答必须能回到原文位置，且不能串到别人的资料。
方案上要求索引可重建、原文权威副本只存在一处，避免同一段文字在两个存储里漂移。

## 存储选型

- 证据片段写入 Qdrant 集合，稠密向量维度为 1024 维，按用户与版本做过滤。
- 原文与定位信息仍保存在 {user["db"]}，向量库只作为可重建索引，随时可以重建。
- 切分粒度按段落聚合，单片段控制在数百字，便于引用时定位行号或页码。
- 每次索引都会记录解析器与切分参数的版本，便于回溯是哪一版参数产出的结果。

## 过滤设计

用户隔离与版本过滤必须写在查询条件里，不能先取回再筛。删除采取软删除加异步
清理：先把资料标记为不可见，再由后台按顺序清理片段、向量与文件，每一步都可
重试，避免出现"文件删了但向量还在"的中间态。

## 索引流程

解析得到文本与位置信息后切分为片段，再逐片段向量化并整体替换该版本的点集合。
替换前会检查该版本是否已经有向量，若有则拒绝原地重建，强制走新版本，防止正在
被引用的旧分析读到被改写的片段。

## 效果

- 通过召回粒度调整与重排，把检索延迟从 180 毫秒降到 40 毫秒。
- 上线后线上问答的无效引用明显减少。
- 索引重建耗时随资料量线性增长，单用户资料重建在秒级完成。

## 遗留问题

召回为空时的解释还不够细，后续需要区分"资料里没有"与"问题问得不清楚"。另外
多版本文档的切换策略还需要与后端确认，避免旧版本被反复召回。
"""


def notes_text(short: str, user: dict, extra: dict) -> str:
    """面试记录：一条字面指标 + 一条只能语义命中的建议。"""
    return f"""# 面试记录 - {user["role"]}（{short}）

## 候选人概况

五年后端经验，最近一段负责交易链路的稳定性，熟悉 {user["stack"]} 与 {user["db"]}。
表达清楚，能用具体数字支撑结论，对线上问题的复盘有方法论，不是停留在"重启解决"。

## 项目追问

追问了缓存与数据库一致性的处理，候选人能说清在读多写少与写多读少两种场景下的
不同取舍，也承认自己在跨服务写场景上偏保守。对幂等与重试的实现细节描述准确，
能指出哪些位置容易漏掉。

## 表现

- 候选人对接口设计的追问回答完整，P99 延迟 87ms 的排查过程讲得清楚。
- 对缓存一致性问题的处理思路偏保守，遇到跨服务写场景时倾向加锁。
- 编码环节完成度较高，边界条件考虑周到，测试习惯良好。
- 对监控的理解到位，能说出哪些指标是真正用于决策的。

## 软技能观察

沟通节奏合适，不抢话也不冷场。面对不确定的问题会先澄清前提，而不是硬答。对
自己不熟悉的领域边界描述清楚，没有夸大经历。

## 面试官建议

- 建议加强分布式事务与最终一致性的实践，补一次真实补偿案例。
- 如果后续承担跨团队项目，需要再确认推动力与沟通方式。
- 整体评价：达到预期，属于可以独立负责中等复杂度项目的水平。
"""


def cert_pdf(short: str, user: dict, extra: dict) -> bytes:
    """三页英文证书 PDF，第二页留空——用于验证空白页之后的页码定位。"""
    pages = [
        f"{extra['cert_name']} - Certificate of Completion. "
        f"Awarded to candidate {short} after completing the assessment.",
        "",
        f"Credential ID {extra['cert_id']}. This credential remains valid through "
        f"{extra['cert_valid']} and covers cloud cost optimization practices.",
    ]
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


EXTRA = {
    "alpha": {
        "stack_version": "FastAPI 0.115.0",
        "db_version": "MySQL 8.0.36",
        "cert_name": "AWS Certified Solutions Architect",
        "cert_id": "AWS-2024-8841",
        "cert_valid": "2027-06",
    },
    "beta": {
        "stack_version": "Spring Boot 3.2.4",
        "db_version": "PostgreSQL 16.2",
        "cert_name": "Oracle Certified Professional Java SE",
        "cert_id": "OCP-2023-5512",
        "cert_valid": "2028-01",
    },
    "gamma": {
        "stack_version": "React 18.2.0",
        "db_version": "IndexedDB (Chromium 122)",
        "cert_name": "Meta Front-End Developer Certificate",
        "cert_id": "META-2024-3390",
        "cert_valid": "2027-09",
    },
}


def build_corpus(short: str, user: dict, extra: dict) -> dict[str, dict[int, bytes]]:
    """返回 doc_key -> {version: raw bytes}，与 DOC_META 的 file_type 配套使用。"""
    return {
        "resume": {1: resume_text(short, user, extra).encode("utf-8")},
        "project_a": {
            1: project_a_v1(short, user, extra).encode("utf-8"),
            2: project_a_v2(short, user, extra).encode("utf-8"),
        },
        "project_b": {1: project_b_text(short, user, extra).encode("utf-8")},
        "notes": {1: notes_text(short, user, extra).encode("utf-8")},
        "cert": {1: cert_pdf(short, user, extra)},
    }


def facts_for(short: str, extra: dict) -> list[Fact]:
    """每个用户 12 条可回答事实：字面与语义各半，覆盖 5 个文档。"""
    return [
        # resume：两条字面 token + 一条语义
        Fact(
            "resume",
            1,
            extra["stack_version"],
            "候选人用的服务框架是哪个版本？",
            "literal",
        ),
        Fact(
            "resume",
            1,
            extra["db_version"],
            "生产环境数据库的具体版本是多少？",
            "literal",
        ),
        Fact(
            "resume",
            1,
            "订单服务的接口设计",
            "这个人在后端主要负责哪类工作？",
            "paraphrase",
        ),
        # project_a v2：新版结论（v1 的同名事实用于旧版本负例）
        Fact(
            "project_a",
            2,
            "缓存命中率 92%",
            "调整策略后缓存命中率到了多少？",
            "literal",
        ),
        Fact(
            "project_a",
            2,
            "每秒约 1200 次请求",
            "压测的吞吐达到了什么水平？",
            "literal",
        ),
        # project_b
        Fact(
            "project_b",
            1,
            "稠密向量维度为 1024 维",
            "向量库里存的是什么维度的向量？",
            "literal",
        ),
        Fact(
            "project_b",
            1,
            "检索延迟从 180 毫秒降到 40 毫秒",
            "他把检索变快了，具体效果如何？",
            "paraphrase",
        ),
        # notes
        Fact("notes", 1, "P99 延迟 87ms", "面试记录里提到他的哪个延迟指标？", "literal"),
        Fact(
            "notes",
            1,
            "建议加强分布式事务",
            "面试官给出的改进建议是什么？",
            "paraphrase",
        ),
        # cert（英文 PDF，第 3 页在第 2 页空白之后）
        Fact("cert", 1, extra["cert_name"], "候选人拿了哪张云相关认证？", "literal"),
        Fact("cert", 1, extra["cert_id"], "这张证书的凭证编号是多少？", "literal"),
        Fact(
            "cert",
            1,
            "cloud cost optimization",
            "这张证书还覆盖了哪方面内容？",
            "paraphrase",
        ),
    ]


def negatives_for(short: str, user: dict, extra: dict, other: str, other_extra: dict):
    """每个用户 8 条负例：旧版本 2、无证据 3、跨用户 2、已删除 1。"""
    return [
        # 旧版本：被取代的 v1 结论不得出现在钉住 v2 的检索结果里；
        # 这里允许返回 v2 的相近 chunk（那是对的），只禁止 v1 的原文。
        Negative(
            "old_version",
            "缓存命中率 60% 是怎么得出的？",
            "v1 结论是 60%，已被 v2 的 92% 取代；钉住 v2 时不得返回 v1 的 chunk。",
            expect="forbidden_absent",
            forbidden_marker="缓存命中率 60%",
            forbidden_doc="project_a",
            forbidden_version=1,
        ),
        Negative(
            "old_version",
            "压测每秒约 300 次请求，瓶颈在哪？",
            "v1 结论是 300 次/秒，已被 v2 的 1200 取代；钉住 v2 时不得返回 v1 的 chunk。",
            expect="forbidden_absent",
            forbidden_marker="每秒约 300 次请求",
            forbidden_doc="project_a",
            forbidden_version=1,
        ),
        # 无证据：语料里根本没有的事实
        Negative(
            "no_evidence",
            "候选人有 Kubernetes 认证吗？",
            "语料中没有任何 Kubernetes 认证记录。",
        ),
        Negative("no_evidence", "候选人写过 Go 语言服务吗？", "语料中不存在 Go 相关内容。"),
        Negative("no_evidence", "候选人发表过论文吗？", "语料中没有任何论文或发表记录。"),
        # 跨用户：问的是别人的事实，发起人是自己。判据是"他人 chunk 不得出现"，
        # 而不是"结果为空"——属主过滤生效后仍会返回自己的无关 chunk，那是正确答案。
        Negative(
            "cross_user",
            f"{other_extra['cert_name']} 是哪年拿的？",
            f"该认证属于 {other} 用户；{short} 的检索不得返回他人的 chunk。",
            expect="forbidden_absent",
            forbidden_marker=other_extra["cert_name"],
            forbidden_doc="cert",
            forbidden_user=other,
        ),
        Negative(
            "cross_user",
            f"{other_extra['stack_version']} 在项目里怎么用的？",
            f"该版本号属于 {other} 用户；{short} 的检索不得返回他人的 chunk。",
            expect="forbidden_absent",
            forbidden_marker=other_extra["stack_version"],
            forbidden_doc="resume",
            forbidden_user=other,
        ),
        # 已删除：purge 后该文档的向量应全部不可检索。判据同样是"禁用 chunk 不得出现"
        # 而不是"结果为空"——库里还有该用户的其它文档，检索返回它们是正确答案。
        Negative(
            "deleted",
            "项目 B 把检索延迟降到了多少？",
            "该文档在评测中被标记删除；物理清理后其全部 chunk 都不得被召回。",
            expect="forbidden_absent",
            forbidden_doc="project_b",
            forbidden_version=1,
            forbid_all=True,
        ),
    ]


def chunk_map(raw: bytes, file_type: str) -> list[str]:
    """按 index_document 的实际切分顺序返回每个 ordinal 的 chunk 原文。

    indexer 对 PDF 是"逐页切分后整体串联"，因此 ordinal 必须按连接后的顺序
    编号，不能用 chunk_document 自己的 ordinal 字段。
    """
    parse_document, chunk_document, _ = rag_modules()
    parsed = parse_document(raw, file_type=file_type)
    if parsed.pages is None:
        return [c.text for c in chunk_document(parsed.lines, max_chars=CHUNK_MAX_CHARS)]
    return [
        c.text for page in parsed.pages for c in chunk_document(page, max_chars=CHUNK_MAX_CHARS)
    ]


def document_id(short: str, doc_key: str) -> str:
    return f"doc_{short}_{doc_key}"


def current_versions(short: str) -> list[tuple[str, int]]:
    """该用户每个文档的当前版本（project_a 已升到 v2），用于模拟 B 的版本快照。"""
    return [
        (document_id(short, "resume"), 1),
        (document_id(short, "project_a"), 2),
        (document_id(short, "project_b"), 1),
        (document_id(short, "notes"), 1),
        (document_id(short, "cert"), 1),
    ]


def resolve_chunk_id(
    short: str,
    doc_key: str,
    version: int,
    marker: str,
    corpus: dict[str, dict[int, bytes]],
) -> str:
    """定位包含 marker 的唯一 chunk，返回与运行期一致的 chunk_id。"""
    filename, file_type = DOC_META[doc_key]
    texts = chunk_map(corpus[doc_key][version], file_type)
    hits = [ordinal for ordinal, text in enumerate(texts) if marker in text]
    if len(hits) != 1:
        raise SystemExit(
            f"{short}/{doc_key}/v{version} 中 marker {marker!r} 命中 {len(hits)} 个 chunk，"
            "标注必须唯一"
        )
    _, _, make_chunk_id = rag_modules()
    return make_chunk_id(document_id(short, doc_key), version, hits[0])


def all_chunk_ids(
    short: str, doc_key: str, version: int, corpus: dict[str, dict[int, bytes]]
) -> list[str]:
    """返回某文档某个版本的全部 chunk_id，用于"整份文档不得出现"的负例。"""
    _, file_type = DOC_META[doc_key]
    texts = chunk_map(corpus[doc_key][version], file_type)
    _, _, make_chunk_id = rag_modules()
    return [
        make_chunk_id(document_id(short, doc_key), version, ordinal)
        for ordinal in range(len(texts))
    ]


def build_questions() -> list[dict]:
    """组装 60 个问题，标注全部指向真实 chunk_id。"""
    questions: list[dict] = []
    shorts = list(USERS)
    corpora = {name: build_corpus(name, USERS[name], EXTRA[name]) for name in shorts}
    for index, short in enumerate(shorts):
        user = USERS[short]
        extra = EXTRA[short]
        corpus = corpora[short]
        other = shorts[(index + 1) % len(shorts)]
        # 与 B 的真实行为一致：传入该用户全部文档的钉住版本，而不是只传目标文档。
        # 只传目标文档会把候选池缩到 2-3 个 chunk，Recall@k 直接饱和、失去区分力。
        versions = [{"document_id": doc_id, "version": v} for doc_id, v in current_versions(short)]

        for i, fact in enumerate(facts_for(short, extra), start=1):
            questions.append(
                {
                    "id": f"q_{short}_{i:02d}",
                    "user_id": user["user_id"],
                    "query": fact.query,
                    "kind": "answerable",
                    "style": fact.style,
                    "relevant_chunk_ids": [
                        resolve_chunk_id(short, fact.doc, fact.version, fact.marker, corpus)
                    ],
                    "document_versions": versions,
                    "note": f"标注依据：{fact.marker} 出现在 {fact.doc} v{fact.version}",
                }
            )

        for i, neg in enumerate(negatives_for(short, user, extra, other, EXTRA[other]), start=1):
            forbidden: list[str] = []
            if neg.forbidden_doc and neg.forbid_all:
                owner = neg.forbidden_user or short
                forbidden.extend(
                    all_chunk_ids(
                        owner,
                        neg.forbidden_doc,
                        neg.forbidden_version,
                        corpora[owner],
                    )
                )
            elif neg.forbidden_marker and neg.forbidden_doc:
                # 跨用户负例的禁用 chunk 在对方语料里，按对方的 short 解析。
                owner = neg.forbidden_user or short
                forbidden.append(
                    resolve_chunk_id(
                        owner,
                        neg.forbidden_doc,
                        neg.forbidden_version,
                        neg.forbidden_marker,
                        corpora[owner],
                    )
                )
            questions.append(
                {
                    "id": f"neg_{short}_{i:02d}",
                    "user_id": user["user_id"],
                    "query": neg.query,
                    "kind": neg.kind,
                    "style": "negative",
                    "expect": neg.expect,
                    "relevant_chunk_ids": [],
                    "forbidden_chunk_ids": forbidden,
                    "document_versions": versions,
                    "note": neg.note,
                }
            )
    return questions


def write_outputs() -> None:
    """写出语料、问题集与清单；已存在的文件按同一内容覆盖，保证可复现。"""
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    documents: list[dict] = []
    for short, user in USERS.items():
        corpus = build_corpus(short, user, EXTRA[short])
        for doc_key, by_version in corpus.items():
            filename, file_type = DOC_META[doc_key]
            # 多版本文档落盘为 <name>.v<N>.<ext> 便于人工检查。
            for version, raw in by_version.items():
                stem, dot, ext = filename.rpartition(".")
                suffix = f".v{version}" if len(by_version) > 1 else ""
                name = f"{short}_{stem}{suffix}{dot}{ext}"
                (CORPUS_DIR / name).write_bytes(raw)
                documents.append(
                    {
                        "document_id": document_id(short, doc_key),
                        "user_id": user["user_id"],
                        "version": version,
                        "path": name,
                        "file_type": file_type,
                    }
                )

    questions = build_questions()
    # 固定 LF：仓库以 LF 为准，Windows 默认写 CRLF 会让每次重新生成都产生差异。
    questions_text = "\n".join(json.dumps(q, ensure_ascii=False) for q in questions) + "\n"
    QUESTIONS_PATH.write_text(questions_text, encoding="utf-8", newline="\n")

    # 清单让评测运行器不必了解生成器的内部结构；同时固定切分参数与问题集指纹，
    # 这样"标注是按哪一版语料与切分产出的"可被运行器直接校验。
    manifest = {
        "chunk_max_chars": CHUNK_MAX_CHARS,
        "questions_sha256": hashlib.sha256(questions_text.encode("utf-8")).hexdigest(),
        "documents": documents,
    }
    (DATA_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def check() -> None:
    """自检：用户数、问题构成、PDF 分页、标注唯一性与负例期望的一致性。"""
    questions = build_questions()
    assert len(questions) == 60, len(questions)
    answerable = [q for q in questions if q["kind"] == "answerable"]
    negatives = [q for q in questions if q["kind"] != "answerable"]
    assert len(answerable) == 36, len(answerable)
    assert len(negatives) == 24, len(negatives)
    kinds: dict[str, int] = {}
    for q in negatives:
        kinds[q["kind"]] = kinds.get(q["kind"], 0) + 1
    assert kinds == {
        "old_version": 6,
        "no_evidence": 9,
        "cross_user": 6,
        "deleted": 3,
    }, kinds
    for q in answerable:
        assert len(q["relevant_chunk_ids"]) == 1, q["id"]

    # 事实必须分散在多个 chunk 上：若多个问题指向同一个 chunk，Recall@k 会虚高，
    # 三档策略的差异也就无从体现。这里要求每个用户至少 9 个互不相同的目标 chunk。
    for short, user in USERS.items():
        targets = {
            q["relevant_chunk_ids"][0] for q in answerable if q["user_id"] == user["user_id"]
        }
        assert len(targets) >= 9, f"{short} 只有 {len(targets)} 个不同的目标 chunk"
    for q in negatives:
        if q["expect"] == "forbidden_absent":
            # 旧版本/跨用户负例禁用单个 chunk；删除负例禁用整份文档的全部 chunk。
            assert q["forbidden_chunk_ids"], q["id"]
        else:
            assert q["forbidden_chunk_ids"] == [], q["id"]
    deleted_forbidden = [len(q["forbidden_chunk_ids"]) for q in negatives if q["kind"] == "deleted"]
    assert deleted_forbidden and min(deleted_forbidden) >= 2, deleted_forbidden

    for short, user in USERS.items():
        corpus = build_corpus(short, user, EXTRA[short])
        for doc_key, by_version in corpus.items():
            _, file_type = DOC_META[doc_key]
            for version, raw in by_version.items():
                count = len(chunk_map(raw, file_type))
                # 单 chunk 文档会让 Recall@k 虚高、三档策略之间看不出差异，
                # 因此语料必须能切出多个 chunk。
                assert count >= 2, f"{short}/{doc_key}/v{version} 只有 {count} 个 chunk"
        pages = PdfReader(BytesIO(corpus["cert"][1])).pages
        assert len(pages) == 3
        assert pages[1].extract_text().strip() == "", "第二页必须是空白页"

    print(
        "PASS: 60 个问题（36 可回答 / 24 负例），"
        f"负例分布 {kinds}，3 份三页 PDF 且第二页空白，标注 chunk_id 唯一可解析。"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 M2 检索评测语料与问题集")
    parser.add_argument("--check", action="store_true", help="只做自检，不写文件")
    args = parser.parse_args()
    if args.check:
        check()
        return
    write_outputs()
    check()


if __name__ == "__main__":
    main()
