import json

import httpx
import pytest

from catalogguard.infrastructure.llm.errors import PermanentLLMError
from catalogguard.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from catalogguard.infrastructure.llm.retrying import RetryingLLM
from catalogguard.infrastructure.llm.schema import listing_json_schema
from catalogguard.infrastructure.metrics import PrometheusMetrics


def _provider(handler) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url="https://api.x.ai/v1",
        api_key="test-key",
        model="grok-4.7",
        timeout=5,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_request_uses_chat_completions_json_schema() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content.decode())
        content = json.dumps({"title": "Fieldwork Pack"})
        return httpx.Response(
            200,
            json={"model": "grok-4.7", "choices": [{"message": {"content": content}}]},
        )

    provider = _provider(handler)
    result = await provider.complete(system="sys", user="user")
    await provider.aclose()

    body = seen["body"]
    assert str(seen["path"]).endswith("/chat/completions")
    assert seen["auth"] == "Bearer test-key"
    assert isinstance(body, dict)
    assert body["model"] == "grok-4.7"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    schema = listing_json_schema()
    assert schema["properties"]["title"]["maxLength"] == 70
    assert schema["properties"]["meta_description"]["maxLength"] == 160
    assert "cited_attributes" in schema["required"]
    assert result.model == "grok-4.7"
    assert "Fieldwork Pack" in result.content


@pytest.mark.asyncio
async def test_transient_status_is_retried() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(
            200,
            json={"model": "grok-4.7", "choices": [{"message": {"content": '{"ok":true}'}}]},
        )

    wrapped = RetryingLLM(
        _provider(handler),
        attempts=3,
        backoff_seconds=0,
        metrics=PrometheusMetrics(),
    )
    result = await wrapped.complete(system="sys", user="user")
    await wrapped.aclose()
    assert calls["n"] == 2
    assert result.content == '{"ok":true}'


@pytest.mark.asyncio
async def test_permanent_status_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad schema")

    wrapped = RetryingLLM(
        _provider(handler),
        attempts=3,
        backoff_seconds=0,
        metrics=PrometheusMetrics(),
    )
    with pytest.raises(PermanentLLMError):
        await wrapped.complete(system="sys", user="user")
    await wrapped.aclose()
    assert calls["n"] == 1
