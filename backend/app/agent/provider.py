"""Bounded chat-completions adapter; no automatic fixture fallback or arbitrary tool calls."""

import asyncio
import json
from typing import Protocol

import httpx
from pydantic import BaseModel, ValidationError

from app.agent.config import AgentSettings
from app.agent.errors import AgentError
from app.agent.models import ModelCompletion, TokenUsage


class AnalysisModel(Protocol):
    async def complete(
        self, *, system: str, payload: dict, output_schema: type[BaseModel]
    ) -> ModelCompletion:
        """Return JSON text and actual model metadata; do not execute model-selected tools."""
        ...


class ChatCompletionModel:
    is_test_double = False

    def __init__(
        self, settings: AgentSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        settings.check_provider()
        self.settings = settings.model_copy(deep=True)
        self.transport = transport

    async def complete(
        self, *, system: str, payload: dict, output_schema: type[BaseModel]
    ) -> ModelCompletion:
        settings = self.settings
        schema = output_schema.model_json_schema()
        body: dict = {
            "model": settings.model,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"data": payload, "output_json_schema": schema}, ensure_ascii=False
                    ),
                },
            ],
            "stream": False,
            "temperature": 0,
            settings.output_token_parameter: settings.max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        if settings.response_format == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": output_schema.__name__,
                    "strict": True,
                    "schema": schema,
                },
            }
        if settings.enable_thinking is not None:
            body["enable_thinking"] = settings.enable_thinking
        headers = {}
        if settings.api_key is not None:
            headers["Authorization"] = f"Bearer {settings.api_key.get_secret_value()}"
        url = settings.base_url.rstrip("/") + "/chat/completions"
        async with httpx.AsyncClient(
            transport=self.transport, timeout=settings.model_timeout_seconds, follow_redirects=False
        ) as client:
            for attempt in range(1, settings.max_attempts + 1):
                try:
                    async with client.stream("POST", url, json=body, headers=headers) as response:
                        if response.status_code in {401, 403}:
                            raise AgentError(
                                "MODEL_AUTH_FAILED", "生成模型认证失败，请检查本机配置。"
                            )
                        if (
                            response.status_code == 429
                            or response.status_code >= 500
                            or response.status_code == 408
                        ):
                            code = (
                                "MODEL_RATE_LIMITED"
                                if response.status_code == 429
                                else "MODEL_UNAVAILABLE"
                            )
                            raise AgentError(code, "生成模型服务暂时不可用。", retryable=True)
                        if response.status_code != 200:
                            raise AgentError(
                                "MODEL_REQUEST_REJECTED",
                                "模型拒绝请求，请核对模型与结构化输出配置。",
                            )
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > settings.max_response_bytes:
                                raise AgentError("MODEL_OUTPUT_INVALID", "模型响应超出允许大小。")
                    return self._parse(bytes(raw), attempt)
                except httpx.TimeoutException:
                    error = AgentError("MODEL_TIMEOUT", "生成模型调用超时。", retryable=True)
                except httpx.TransportError:
                    error = AgentError(
                        "MODEL_UNAVAILABLE", "无法连接生成模型服务。", retryable=True
                    )
                except AgentError as exc:
                    if not exc.retryable:
                        raise
                    error = exc
                if attempt == settings.max_attempts:
                    raise error
                await asyncio.sleep(settings.retry_delay_seconds * 2 ** (attempt - 1))
        raise AgentError("MODEL_UNAVAILABLE", "生成模型未返回结果。")

    @staticmethod
    def _parse(raw: bytes, attempts: int) -> ModelCompletion:
        try:
            response = json.loads(raw)
            if not isinstance(response, dict):
                raise ValueError("Expected response object")
            choices = response["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("Expected one completion")
            choice = choices[0]
            if not isinstance(choice, dict):
                raise ValueError("Expected choice object")
            message = choice["message"]
            if not isinstance(message, dict):
                raise ValueError("Expected message object")
            if (
                choice.get("finish_reason") != "stop"
                or message.get("refusal")
                or message.get("tool_calls")
                or message.get("function_call")
            ):
                raise ValueError("Incomplete, refused or tool-calling completion")
            content, version = message["content"], response["model"]
            if not isinstance(content, str) or not isinstance(version, str):
                raise ValueError("Expected text completion and actual model version")
            usage = response.get("usage") or {}
            if not isinstance(usage, dict):
                raise ValueError("Expected usage object")
            return ModelCompletion(
                content=content,
                model_version=version,
                attempts=attempts,
                usage=TokenUsage.model_validate(
                    {
                        "input_tokens": usage.get("prompt_tokens", 0),
                        "output_tokens": usage.get("completion_tokens", 0),
                    },
                    strict=True,
                ),
            )
        except (ValueError, TypeError, KeyError, IndexError, ValidationError):
            raise AgentError("MODEL_OUTPUT_INVALID", "模型响应不完整或不符合约定。") from None
