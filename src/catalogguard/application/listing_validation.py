"""Parse and validate listing JSON. Violations are returned, never dropped."""

import json
import re

from catalogguard.domain.listing_rules import LISTING_LIMITS, format_attribute_value

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_CITATION = re.compile(r"\[\[([a-zA-Z0-9_]+)\]\]")
_MARKER = re.compile(r"\s*\[\[[a-zA-Z0-9_]+\]\]")
_VALUE_WINDOW = 48


class MalformedLLMOutputError(Exception):
    """The model did not return a JSON object. The bus may retry this."""

    def __init__(self, message: str, *, raw: str = "") -> None:
        snippet = raw.strip().replace("\n", " ")[:500]
        detail = f"{message}: {snippet}" if snippet else message
        super().__init__(detail)
        self.raw = raw


def parse_llm_json(raw: str) -> dict[str, object]:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = _FENCE.sub("", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise MalformedLLMOutputError("LLM output is not valid JSON", raw=raw) from exc
    if not isinstance(parsed, dict):
        raise MalformedLLMOutputError("LLM output must be a JSON object", raw=raw)
    return parsed


def _value_token(value: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])", re.IGNORECASE)


def render_customer_copy(text: str) -> str:
    """Drop citation markers. Drafts keep them; live listings do not."""

    return " ".join(_MARKER.sub("", text).split())


def _citations_have_value(text: str, key: str, value: str) -> bool:
    """True when every [[key]] in this field has the value just before it."""

    if value == "":
        return True
    token = _value_token(value)
    for match in _CITATION.finditer(text):
        if match.group(1) != key:
            continue
        start = max(0, match.start() - _VALUE_WINDOW)
        if token.search(text[start : match.start()]) is None:
            return False
    return True


def validate_listing(data: dict[str, object], attributes: dict[str, object]) -> list[str]:
    """Return human-readable violations. An empty list means the draft is acceptable."""

    violations: list[str] = []
    texts: list[str] = []
    for field, limit in LISTING_LIMITS.items():
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            violations.append(f"invalid:{field}")
            continue
        if len(value) > limit:
            violations.append(f"max_length:{field}:{len(value)}>{limit}")
        texts.append(value)

    cited_raw = data.get("cited_attributes")
    cited: list[str] = []
    if not isinstance(cited_raw, list) or not all(isinstance(item, str) for item in cited_raw):
        violations.append("invalid:cited_attributes")
    else:
        cited = list(cited_raw)

    known = set(attributes)
    for key in cited:
        if key not in known:
            violations.append(f"unknown_attribute:{key}")

    combined = "\n".join(texts)
    mentioned = _CITATION.findall(combined)
    unknown_citations = {token for token in mentioned if token not in known}
    for token in sorted(unknown_citations):
        violations.append(f"unknown_citation:{token}")

    mentioned_set = set(mentioned)
    cited_set = set(cited)
    for key in cited:
        if key not in mentioned_set:
            violations.append(f"cited_not_referenced:{key}")
        elif key in known and any(
            not _citations_have_value(field, key, format_attribute_value(attributes[key]))
            for field in texts
        ):
            violations.append(f"value_not_in_text:{key}")
    for token in mentioned_set:
        if token in known and token not in cited_set:
            violations.append(f"referenced_not_cited:{token}")
    return violations


def text_field(data: dict[str, object], key: str) -> str:
    value = data.get(key, "")
    return value if isinstance(value, str) else ""
