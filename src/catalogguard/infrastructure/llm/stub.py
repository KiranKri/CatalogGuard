"""Deterministic listing copy. No network and no API key.

The stub reads the same prompt blocks the real provider sees, so a prompt
change that drops the product payload fails here instead of in production.
"""

import json

from catalogguard.domain.listing_rules import LISTING_LIMITS
from catalogguard.infrastructure.llm.errors import PermanentLLMError
from catalogguard.infrastructure.llm.types import LLMResult


def _line(text: str, prefix: str) -> str:
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    raise PermanentLLMError(f"Prompt is missing {prefix.rstrip('=')}")


def _clip(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip()


class StubLLM:
    """Stable copy used by default and by every test that does not inject a fake."""

    model_name = "stub-deterministic-v1"

    async def complete(self, *, system: str, user: str) -> LLMResult:
        del system
        product = json.loads(_line(user, "PRODUCT_JSON="))
        store = json.loads(_line(user, "STORE_JSON="))
        values = json.loads(_line(user, "ATTRIBUTE_VALUES="))
        if not isinstance(product, dict) or not isinstance(store, dict):
            raise PermanentLLMError("Prompt product or store block is not an object")
        if not isinstance(values, dict):
            raise PermanentLLMError("Prompt attribute values are not an object")
        rendered = _copy(product, store, {str(key): str(val) for key, val in values.items()})
        return LLMResult(
            content=json.dumps(rendered, separators=(",", ":")),
            model=self.model_name,
        )

    async def aclose(self) -> None:
        return None


def _copy(product: dict[str, object], store: dict[str, object], values: dict[str, str]) -> dict:
    brand = str(product.get("brand", "")).strip()
    name = str(product.get("name", "")).strip()
    store_name = str(store.get("name", "")).strip()
    base = str(product.get("base_description", "")).strip()
    chosen = sorted(key for key, value in values.items() if value)
    chosen = chosen[:4]
    while True:
        clause = " ".join(f"{values[key]} [[{key}]]" for key in chosen)
        title = _clip(f"{brand} {name}".strip(), LISTING_LIMITS["title"])
        short = _clip(
            f"{brand} {name} at {store_name}. {clause}".strip(),
            LISTING_LIMITS["short_description"],
        )
        long = _clip(f"{short} {base}".strip(), LISTING_LIMITS["long_description"])
        meta_title = _clip(title, LISTING_LIMITS["meta_title"])
        meta_description = _clip(
            f"{brand} {name} at {store_name}.",
            LISTING_LIMITS["meta_description"],
        )
        blob = "\n".join([title, short, long, meta_title, meta_description])
        ok = all(f"[[{key}]]" in blob and values[key] in blob for key in chosen)
        if ok or not chosen:
            if not ok:
                chosen = []
                short = _clip(
                    f"{brand} {name} at {store_name}.".strip(),
                    LISTING_LIMITS["short_description"],
                )
                long = _clip(f"{short} {base}".strip(), LISTING_LIMITS["long_description"])
            return {
                "title": title or name or brand or "Listing",
                "short_description": short or title or "Store listing",
                "long_description": long or short or title or "Store listing",
                "meta_title": meta_title or title or "Listing",
                "meta_description": meta_description or meta_title or "Store listing",
                "cited_attributes": chosen,
            }
        chosen = chosen[:-1]
