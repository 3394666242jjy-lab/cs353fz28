"""公共知识 API 契约（M3.2，issue #41）。

查询返回的引用复用 ``app.rag.knowledge.KnowledgeReference``——它与 dev/16 的
ResearchContext 是同一契约，D 侧（#39）的学习参考通道直接消费这个结构，
保证公共引用只作 ``reference_only``、不能进入个人证据。
"""

from pydantic import Field

from app.rag.knowledge import KnowledgeReference, Role
from app.schemas.common import ContractModel


class KnowledgeSourceResult(ContractModel):
    """一个已发布公共知识来源的版本快照（列表接口返回，不含正文）。"""

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str
    roles: list[Role] = Field(min_length=1)
    topics: list[str] = Field(min_length=1)
    version: str
    verified_on: str
    content_kind: str
    usage: str


class KnowledgeSourcePage(ContractModel):
    items: list[KnowledgeSourceResult]


class KnowledgeQueryIn(ContractModel):
    """公共知识引用查询入参；岗位与意图由服务端约束（dev/16），模型不能改写。"""

    role: Role
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=4, ge=1, le=8)
    max_chars: int = Field(default=6000, ge=500, le=16000)


class KnowledgeQueryResult(ContractModel):
    """引用查询结果：无命中时 items 为空列表（保留空引用，不编造来源）。"""

    items: list[KnowledgeReference]
    notice: str = "公共资料仅供知识解释与学习建议，不能证明用户具有某项经历。"
