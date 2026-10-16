import pytest

from catalogguard.application.listing_validation import (
    MalformedLLMOutputError,
    parse_llm_json,
    validate_listing,
)


def _valid() -> dict:
    return {
        "title": "Trailhead Pack",
        "short_description": "Color olive [[color]].",
        "long_description": "A day pack in olive [[color]].",
        "meta_title": "Trailhead Pack",
        "meta_description": "Olive day pack.",
        "cited_attributes": ["color"],
    }


def test_valid_listing_has_no_violations() -> None:
    assert validate_listing(_valid(), {"color": "olive"}) == []


def test_title_over_70_characters_is_flagged() -> None:
    data = _valid()
    data["title"] = "T" * 71
    violations = validate_listing(data, {"color": "olive"})
    assert "max_length:title:71>70" in violations


def test_unknown_attribute_is_flagged_and_not_accepted() -> None:
    data = _valid()
    data["short_description"] = "Waterproof [[waterproof_rating]]."
    data["long_description"] = "Claims [[waterproof_rating]]."
    data["cited_attributes"] = ["waterproof_rating"]
    violations = validate_listing(data, {"color": "olive"})
    assert "unknown_attribute:waterproof_rating" in violations
    assert "unknown_citation:waterproof_rating" in violations


def test_missing_field_is_flagged() -> None:
    data = _valid()
    del data["meta_description"]
    violations = validate_listing(data, {"color": "olive"})
    assert "invalid:meta_description" in violations


def test_cited_value_must_appear() -> None:
    data = _valid()
    data["short_description"] = "Color [[color]]."
    data["long_description"] = "A day pack [[color]]."
    data["meta_description"] = "A day pack."
    violations = validate_listing(data, {"color": "olive"})
    assert "value_not_in_text:color" in violations


def test_parse_strips_json_fences() -> None:
    parsed = parse_llm_json('```json\n{"title": "Ok"}\n```')
    assert parsed == {"title": "Ok"}


def test_parse_rejects_prose() -> None:
    with pytest.raises(MalformedLLMOutputError):
        parse_llm_json("not json at all")
