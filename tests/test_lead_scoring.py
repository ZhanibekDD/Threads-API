from app.lead_scoring import LeadSignals, decide, score_lead


def test_hot_lead_scores_high():
    signals = LeadSignals(
        own_current_problem=True,
        restriction_active_now=True,
        kazakhstan_signals=True,
        asks_what_to_do=True,
        fresh_post=True,
        has_bank_or_1414_keyword=True,
        explicitly_asks_for_help=True,
    )
    assert score_lead(signals) == 100  # clamped


def test_news_or_ad_is_penalized_to_zero():
    signals = LeadSignals(own_current_problem=True, is_news_vacancy_meme_ad=True)
    # 40 - 50 -> negative, clamped to 0
    assert score_lead(signals) == 0


def test_fraud_flag_clamps_to_zero():
    signals = LeadSignals(own_current_problem=True, restriction_active_now=True,
                           fraud_or_illegal_request=True)
    assert score_lead(signals) == 0


def test_no_signals_scores_zero():
    assert score_lead(LeadSignals()) == 0


def test_decide_thresholds():
    assert decide(50, manual_review_min=70, auto_publish_min=90) == "skip"
    assert decide(69, manual_review_min=70, auto_publish_min=90) == "skip"
    assert decide(70, manual_review_min=70, auto_publish_min=90) == "manual_review"
    assert decide(89, manual_review_min=70, auto_publish_min=90) == "manual_review"
    assert decide(90, manual_review_min=70, auto_publish_min=90) == "auto_publish"
    assert decide(100, manual_review_min=70, auto_publish_min=90) == "auto_publish"
