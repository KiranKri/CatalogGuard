"""JSON schema sent to OpenAI-compatible structured outputs."""

from catalogguard.domain.listing_rules import LISTING_LIMITS


def listing_json_schema() -> dict[str, object]:
    properties: dict[str, object] = {}
    for field, limit in LISTING_LIMITS.items():
        properties[field] = {"type": "string", "minLength": 1, "maxLength": limit}
    properties["cited_attributes"] = {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "maxLength": 64},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": [*LISTING_LIMITS.keys(), "cited_attributes"],
    }
