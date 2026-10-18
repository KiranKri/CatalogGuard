"""OpenAI-compatible Chat Completions provider.

When LLM_PROVIDER=openai, the example base URL is xAI at https://api.x.ai/v1
and the example model is grok-4.7, as listed in the xAI docs in October 2026.
Any server that accepts
`POST /chat/completions` with `response_format.type = json_schema` works.
Set LLM_BASE_URL and LLM_API_KEY (or XAI_API_KEY). The stub provider stays
the default so the service boots with no key.
"""

import json

import httpx

from catalogguard.infrastructure.llm.errors import PermanentLLMError, TransientLLMError
from catalogguard.infrastructure.llm.schema import listing_json_schema
from catalogguard.infrastructure.llm.types import LLMResult

_RETRYABLE = {408, 409, 429, 500, 502, 503, 504}


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(self, *, system: str, user: str) -> LLMResult:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "listing_copy",
                    "strict": True,
                    "schema": listing_json_schema(),
                },
            },
        }
        try:
            response = await self._client.post("/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise TransientLLMError("LLM request timed out") from exc
        except httpx.TransportError as exc:
            raise TransientLLMError("LLM request failed") from exc
        if response.status_code in _RETRYABLE:
            raise TransientLLMError(f"LLM HTTP {response.status_code}")
        if response.status_code >= 400:
            detail = response.text[:300]
            raise PermanentLLMError(f"LLM HTTP {response.status_code}: {detail}")
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise TransientLLMError("LLM response was not JSON") from exc
        content = _message_content(body)
        model = body.get("model") if isinstance(body, dict) else None
        return LLMResult(content=content, model=str(model or self._model))


def _message_content(body: object) -> str:
    if not isinstance(body, dict):
        raise TransientLLMError("LLM response was not an object")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise TransientLLMError("LLM response did not include choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise TransientLLMError("LLM response did not include a message")
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        joined = "".join(parts).strip()
        if joined:
            return joined
    raise TransientLLMError("LLM response content was empty")
