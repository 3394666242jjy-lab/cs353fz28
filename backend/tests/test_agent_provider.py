import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.agent.config import AgentSettings
from app.agent.errors import AgentError
from app.agent.models import RequirementBatch
from app.agent.provider import ChatCompletionModel


def settings(**kwargs):
    return AgentSettings(
        _env_file=None,
        base_url="https://model.example.invalid/v1",
        model="synthetic-model",
        api_key=SecretStr("synthetic-test-key"),
        retry_delay_seconds=0,
        **kwargs,
    )


def response(**overrides):
    return {
        "model": "actual-model-snapshot",
        "choices": [{"finish_reason": "stop", "message": {"content": '{"requirements": []}'}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 6},
        **overrides,
    }


def call(handler, config=None):
    model = ChatCompletionModel(config or settings(), transport=httpx.MockTransport(handler))
    return asyncio.run(
        model.complete(
            system="Return JSON only.",
            payload={"raw_jd": "合成 JD"},
            output_schema=RequirementBatch,
        )
    )


def test_request_shape_and_actual_model_metadata():
    def handler(request):
        assert str(request.url) == "https://model.example.invalid/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer synthetic-test-key"
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert body["stream"] is False
        assert body["max_completion_tokens"] == 4096
        assert "tools" not in body and "user_id" not in request.content.decode()
        assert "output_json_schema" in json.loads(body["messages"][1]["content"])
        return httpx.Response(200, json=response())

    completion = call(handler)
    assert completion.model_version == "actual-model-snapshot"
    assert completion.usage.input_tokens == 12 and completion.usage.output_tokens == 6
    assert "requirements" not in repr(completion)


def test_json_schema_and_thinking_options():
    def handler(request):
        body = json.loads(request.content)
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["enable_thinking"] is False
        return httpx.Response(200, json=response())

    call(handler, settings(response_format="json_schema", enable_thinking=False))


def test_explicit_legacy_token_parameter_for_older_compatible_models():
    def handler(request):
        body = json.loads(request.content)
        assert body["max_tokens"] == 4096
        assert "max_completion_tokens" not in body
        return httpx.Response(200, json=response())

    call(handler, settings(output_token_parameter="max_tokens", enable_thinking=False))


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_only_transient_http_failures_are_retried_with_a_limit(status):
    requests = []

    def handler(request):
        requests.append(request)
        return (
            httpx.Response(status, text="PRIVATE-PROVIDER-RESPONSE")
            if len(requests) == 1
            else httpx.Response(200, json=response())
        )

    assert call(handler).attempts == 2
    assert len(requests) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 302])
def test_auth_input_and_redirect_errors_fail_without_retry_or_body_echo(status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            text="PRIVATE-PROVIDER-RESPONSE",
            headers={"location": "https://other.example.invalid"},
        )

    with pytest.raises(AgentError) as caught:
        call(handler)
    assert len(requests) == 1
    assert "PRIVATE" not in str(caught.value)
    assert "synthetic-test-key" not in str(caught.value)
    assert caught.value.retryable is False


def test_transient_failure_stops_after_two_attempts():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, text="private")

    with pytest.raises(AgentError) as caught:
        call(handler)
    assert caught.value.code == "MODEL_RATE_LIMITED"
    assert len(requests) == 2


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"choices": []},
        response(choices=[[]]),
        response(choices=[{"message": []}]),
        response(usage=[1]),
        response(choices=[{"finish_reason": "length", "message": {"content": "partial"}}]),
        response(
            choices=[{"finish_reason": "stop", "message": {"content": "{}", "refusal": "refused"}}]
        ),
        response(
            choices=[
                {
                    "finish_reason": "stop",
                    "message": {"content": "{}", "tool_calls": [{"name": "run_shell"}]},
                }
            ]
        ),
        response(choices=[{"finish_reason": "stop", "message": {"content": None}}]),
        response(usage={"prompt_tokens": -1}),
    ],
)
def test_incomplete_refused_or_tool_calling_results_are_not_accepted(data):
    with pytest.raises(AgentError) as caught:
        call(lambda request: httpx.Response(200, json=data))
    assert caught.value.code == "MODEL_OUTPUT_INVALID"


def test_oversized_response_is_rejected():
    with pytest.raises(AgentError) as caught:
        call(
            lambda request: httpx.Response(200, text="x" * 2048), settings(max_response_bytes=1024)
        )
    assert caught.value.code == "MODEL_OUTPUT_INVALID"


def test_missing_config_and_insecure_remote_endpoint_fail_before_network():
    with pytest.raises(AgentError) as caught:
        ChatCompletionModel(AgentSettings(_env_file=None))
    assert caught.value.code == "MODEL_NOT_CONFIGURED"
    with pytest.raises(AgentError):
        ChatCompletionModel(
            AgentSettings(
                _env_file=None,
                base_url="http://remote.example.invalid/v1",
                model="model",
                require_api_key=False,
            )
        )
    ChatCompletionModel(
        AgentSettings(
            _env_file=None,
            base_url="http://127.0.0.1:11434/v1",
            model="model",
            require_api_key=False,
        )
    )
    with pytest.raises(ValidationError):
        AgentSettings(_env_file=None, max_tool_calls=1, max_requirements=3)
