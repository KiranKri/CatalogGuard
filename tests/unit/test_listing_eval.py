import json
from pathlib import Path

from catalogguard.application.listing_validation import validate_listing

_CASES = Path(__file__).resolve().parents[2] / "evals" / "listing_guardrail.json"


def test_listing_guardrail_eval_set() -> None:
    cases = json.loads(_CASES.read_text(encoding="utf-8"))
    assert cases, "eval set is empty"
    for case in cases:
        violations = validate_listing(case["payload"], case["attributes"])
        expected = case.get("expect_violations")
        if expected is not None:
            assert set(violations) == set(expected), (case["name"], violations)
        for prefix in case.get("expect_prefixes", []):
            assert any(item.startswith(prefix) for item in violations), case["name"]
