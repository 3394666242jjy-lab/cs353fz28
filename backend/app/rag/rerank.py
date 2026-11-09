"""Rerank 适配器（RAG 模块，成员 C，issue #25）。

混合检索先召回一批候选，再由 Rerank 服务对「查询 - 候选原文」逐对打分重排。本模块是
全项目唯一知道 Rerank provider 协议的地方，换服务商只改这里。

线上协议为阿里云百炼的原生 rerank 端点（**不是** OpenAI 兼容端点，兼容端点没有
/rerank）：

    POST {RERANK_URL}
    {"model": "...", "input": {"query": q, "documents": [...]},
     "parameters": {"return_documents": false, "top_n": n}}
    -> {"output": {"results": [{"index": i, "relevance_score": 0.96}, ...]}}

鉴权与 Embedding 共用同一个 API Key：``RERANK_API_KEY`` 留空时回退到
``EMBEDDING_API_KEY``，避免同一个密钥在两处各配一遍。

重排需要候选**原文**，而原文权威副本在 MongoDB（成员 B 的数据域），C 只持有 chunk_id。
因此重排不自己查库，候选文本由调用方注入（见 retrieval 的 text_resolver）：拿不到原文
的候选不能打分，必须被排除并给出告警，不能假装它排在后面。
"""

import asyncio
from typing import Protocol

import httpx

from app.config import Settings, get_settings

# 错误信息中摘录 provider 原因的最大字符数，防止把整页响应写进日志或异常。
ERROR_EXCERPT_CHARS = 200


class RerankError(RuntimeError):
    """Rerank 调用失败。``retryable`` 标记可重试的瞬时错误（限流/服务端/网络）。"""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class RerankProvider(Protocol):
    """Rerank 能力协议：调用方只依赖接口，不关心具体服务商。"""

    async def rerank(
        self, *, query: str, documents: list[str], top_n: int | None = None
    ) -> list[tuple[int, float]]:
        """对候选打分并按相关度降序返回 (原始下标, 分数)。"""
        ...

    @property
    def model_version(self) -> str:
        """provider 模型标识，随检索配置版本一起留档。"""
        ...


class DashScopeRerankProvider:
    """基于阿里云百炼原生 rerank 端点的适配器。

    ``transport`` 仅供测试注入 MockTransport，生产路径传空走默认传输。
    """

    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        # 与 Embedding 共用密钥：RERANK_API_KEY 留空时回退，避免重复配置。
        secret = settings.rerank_api_key or settings.embedding_api_key
        self._api_key = secret.get_secret_value() if secret else None
        self._url = settings.rerank_url
        self._model = settings.rerank_model
        self._timeout = settings.rerank_timeout_seconds
        self._max_retries = settings.rerank_max_retries
        self._backoff = settings.rerank_retry_backoff_seconds
        self._transport = transport

    @property
    def model_version(self) -> str:
        return self._model

    async def rerank(
        self, *, query: str, documents: list[str], top_n: int | None = None
    ) -> list[tuple[int, float]]:
        """对候选打分，返回按分数降序的 (下标, 分数) 列表。

        空候选直接返回空，不调用服务；结果数量必须与输入一致，否则视为协议错误——
        静默丢掉候选会让重排看起来"更准"，实际是少比了几个。
        """
        if not documents:
            return []
        if not query.strip():
            raise ValueError("重排查询文本不能为空白")
        last_error: RerankError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await self._call(query=query, documents=documents, top_n=top_n)
            except RerankError as error:
                if not error.retryable or attempt == self._max_retries:
                    raise
                last_error = error
                await asyncio.sleep(self._backoff * (2**attempt))
        raise last_error  # pragma: no cover - 循环要么返回要么抛出

    async def _call(
        self, *, query: str, documents: list[str], top_n: int | None
    ) -> list[tuple[int, float]]:
        """执行一次 HTTP 调用并把响应转换成按分数降序的下标与分数。"""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        parameters: dict = {"return_documents": False}
        if top_n is not None:
            parameters["top_n"] = top_n
        payload = {
            "model": self._model,
            "input": {"query": query, "documents": documents},
            "parameters": parameters,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(self._url, headers=headers, json=payload)
        except httpx.TimeoutException as error:
            raise RerankError(f"rerank 请求超时（{self._timeout}s）", retryable=True) from error
        except httpx.HTTPError as error:
            raise RerankError(f"rerank 传输失败：{type(error).__name__}", retryable=True) from error
        if response.status_code == 429:
            raise RerankError("rerank provider 限流", retryable=True)
        if response.status_code >= 500:
            raise RerankError(f"rerank provider 错误：HTTP {response.status_code}", retryable=True)
        if response.status_code >= 400:
            raise RerankError(
                f"rerank 请求被拒绝：HTTP {response.status_code} {_provider_message(response)}",
                retryable=False,
            )
        try:
            results = response.json()["output"]["results"]
            scored = sorted(
                ((int(item["index"]), float(item["relevance_score"])) for item in results),
                key=lambda pair: (-pair[1], pair[0]),
            )
        except (ValueError, KeyError, TypeError) as error:
            raise RerankError("rerank 响应缺少 output.results 字段", retryable=False) from error
        # 指定 top_n 时 provider 会截断返回，这是协议行为；未指定时必须逐条打分。
        expected = len(documents) if top_n is None else min(top_n, len(documents))
        if len(scored) != expected:
            raise RerankError(
                f"rerank 返回 {len(scored)} 条结果，与预期 {expected} 条不一致",
                retryable=False,
            )
        if any(index < 0 or index >= len(documents) for index, _ in scored):
            raise RerankError("rerank 返回的下标越界", retryable=False)
        return scored


def _provider_message(response: httpx.Response) -> str:
    """摘录 provider 的错误原因，长度受限。"""
    try:
        body = response.json()
    except ValueError:
        return response.text[:ERROR_EXCERPT_CHARS]
    message = body.get("message") or body.get("code") or ""
    return str(message)[:ERROR_EXCERPT_CHARS]


def get_rerank_provider(
    settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> RerankProvider | None:
    """工厂：未配置模型时返回 None，由调用方决定报错而不是静默跳过重排。

    这里刻意不抛异常：配置缺失属于部署选择，而"该报错还是该跳过"取决于调用方语义
    （例如 dense 档位根本不需要重排）。
    """
    settings = settings or get_settings()
    if not settings.rerank_model or not settings.rerank_url:
        return None
    return DashScopeRerankProvider(settings, transport=transport)
