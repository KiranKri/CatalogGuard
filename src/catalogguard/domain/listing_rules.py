"""Copy rules for generated listings.

Lengths are business rules. The LLM schema and the validator both read them.
The guardrail is structural: cited attribute keys must exist on the product,
every cited key must appear as [[key]] in the copy, and the formatted attribute
value must appear as its own token. This does not prove that free prose is
entailed by the catalog. Uncited claims are a known gap; the prompt forbids
them and unknown [[key]] markers are rejected.
"""

LISTING_LIMITS: dict[str, int] = {
    "title": 70,
    "short_description": 300,
    "long_description": 2000,
    "meta_title": 70,
    "meta_description": 160,
}

CITATION_FIELDS = tuple(LISTING_LIMITS)


def format_attribute_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
