"""Fill prompt templates. Tokens are ``{{NAME}}`` so product text can contain braces."""

import json
from dataclasses import dataclass

from catalogguard.domain.listing_rules import LISTING_LIMITS, format_attribute_value
from catalogguard.domain.models import Product, Store


@dataclass(frozen=True)
class PromptTemplate:
    prompt_version: str
    system: str
    user: str


def fill_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def _dumps(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def render_email(template: str, values: dict[str, str]) -> tuple[str, str]:
    filled = fill_template(template, values)
    if "SUBJECT:" not in filled or "BODY:" not in filled:
        raise ValueError("Email template must contain SUBJECT and BODY")
    head, body = filled.split("BODY:", 1)
    subject = head.split("SUBJECT:", 1)[1].strip()
    return subject, body.strip()


def listing_prompt_values(product: Product, store: Store) -> dict[str, str]:
    attributes = {key: product.attributes[key] for key in sorted(product.attributes)}
    formatted = {key: format_attribute_value(attributes[key]) for key in attributes}
    product_payload = {
        "sku": product.sku,
        "name": product.name,
        "brand": product.brand,
        "category": product.category,
        "base_description": product.base_description,
        "attributes": attributes,
    }
    store_payload = {"id": store.id, "name": store.name, "region": store.region}
    return {
        "TITLE_MAX": str(LISTING_LIMITS["title"]),
        "SHORT_MAX": str(LISTING_LIMITS["short_description"]),
        "LONG_MAX": str(LISTING_LIMITS["long_description"]),
        "META_TITLE_MAX": str(LISTING_LIMITS["meta_title"]),
        "META_DESCRIPTION_MAX": str(LISTING_LIMITS["meta_description"]),
        "STORE_JSON": _dumps(store_payload),
        "PRODUCT_JSON": _dumps(product_payload),
        "ATTRIBUTE_KEYS": _dumps(list(attributes)),
        "ATTRIBUTE_VALUES": _dumps(formatted),
    }
