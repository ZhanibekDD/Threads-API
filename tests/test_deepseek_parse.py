import json

from app.deepseek_client import FALLBACK_RESULT, parse_deepseek_json

VALID = {
    "relevant": True,
    "decision": "manual_review",
    "language": "ru",
    "problem_type": "account_arrest",
    "lead_score": 82,
    "urgency": "high",
    "kazakhstan_probability": 90,
    "risk_flags": [],
    "comment": "Проверьте, кто наложил арест. zakonexpertt.kz +7 775 299-87-38",
    "reason": "active kaspi arrest",
}


def test_parses_valid_json():
    result = parse_deepseek_json(json.dumps(VALID))
    assert result["relevant"] is True
    assert result["lead_score"] == 82
    assert result["decision"] == "manual_review"


def test_empty_response_returns_fallback():
    assert parse_deepseek_json(None) == FALLBACK_RESULT
    assert parse_deepseek_json("") == FALLBACK_RESULT


def test_malformed_json_returns_fallback():
    assert parse_deepseek_json("{not valid json") == FALLBACK_RESULT


def test_missing_required_field_returns_fallback():
    broken = dict(VALID)
    del broken["lead_score"]
    assert parse_deepseek_json(json.dumps(broken)) == FALLBACK_RESULT


def test_score_out_of_range_is_clamped():
    over = dict(VALID, lead_score=150, kazakhstan_probability=-10)
    result = parse_deepseek_json(json.dumps(over))
    assert result["lead_score"] == 100
    assert result["kazakhstan_probability"] == 0


def test_invalid_decision_falls_back_to_skip():
    bad_decision = dict(VALID, decision="publish_now_please")
    result = parse_deepseek_json(json.dumps(bad_decision))
    assert result["decision"] == "skip"


def test_comment_is_truncated_to_420_chars():
    long_comment = dict(VALID, comment="а" * 1000)
    result = parse_deepseek_json(json.dumps(long_comment))
    assert len(result["comment"]) == 420
