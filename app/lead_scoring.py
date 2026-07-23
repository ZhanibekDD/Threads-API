from dataclasses import dataclass, fields


@dataclass(frozen=True)
class LeadSignals:
    """Boolean signals extracted from a post (typically by DeepSeek)."""

    own_current_problem: bool = False
    restriction_active_now: bool = False
    kazakhstan_signals: bool = False
    asks_what_to_do: bool = False
    fresh_post: bool = False
    has_bank_or_1414_keyword: bool = False
    explicitly_asks_for_help: bool = False

    is_news_vacancy_meme_ad: bool = False
    is_lawyer_or_competitor: bool = False
    not_kazakhstan: bool = False
    general_discussion_no_personal_problem: bool = False
    fraud_or_illegal_request: bool = False


_POINTS = {
    "own_current_problem": 40,
    "restriction_active_now": 25,
    "kazakhstan_signals": 15,
    "asks_what_to_do": 10,
    "fresh_post": 10,
    "has_bank_or_1414_keyword": 10,
    "explicitly_asks_for_help": 10,
    "is_news_vacancy_meme_ad": -50,
    "is_lawyer_or_competitor": -50,
    "not_kazakhstan": -40,
    "general_discussion_no_personal_problem": -30,
    "fraud_or_illegal_request": -100,
}


def score_lead(signals: LeadSignals) -> int:
    total = 0
    for f in fields(signals):
        if getattr(signals, f.name):
            total += _POINTS[f.name]
    return max(0, min(100, total))


def decide(score: int, manual_review_min: int, auto_publish_min: int) -> str:
    """Classify a score into skip / manual_review / auto_publish.

    Note: "auto_publish" here is only a *priority label* used by the review
    queue (e.g. to surface it first / pre-check the approve button in
    Telegram). No code path in this project posts to Threads without an
    explicit human confirmation — see pipeline.publish_reply().
    """
    if score >= auto_publish_min:
        return "auto_publish"
    if score >= manual_review_min:
        return "manual_review"
    return "skip"
