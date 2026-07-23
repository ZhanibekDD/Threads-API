import logging

from app.logging_utils import SecretRedactingFilter


def _filtered_message(raw: str) -> str:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, raw, (), None)
    SecretRedactingFilter().filter(record)
    return record.getMessage()


def test_redacts_access_token_in_url():
    msg = _filtered_message("GET https://graph.threads.net/me?access_token=THAAsupersecret123 200 OK")
    assert "THAAsupersecret123" not in msg
    assert "[REDACTED]" in msg


def test_redacts_oauth_code_in_url():
    msg = _filtered_message("GET /threads/callback?code=AQBxyzsecretcode&state=abc 302")
    assert "AQBxyzsecretcode" not in msg
    assert "[REDACTED]" in msg
    assert "state=abc" in msg  # state is not secret, should stay visible for debugging


def test_redacts_client_secret():
    msg = _filtered_message("POST body client_secret=deadbeefcafebabe1234567890abcdef")
    assert "deadbeefcafebabe1234567890abcdef" not in msg


def test_redacts_telegram_bot_token():
    msg = _filtered_message(
        "GET https://api.telegram.org/bot1234567890:AAFakeTokenNotRealDoNotUse00000000/sendMessage 200"
    )
    assert "AAFakeTokenNotRealDoNotUse00000000" not in msg


def test_leaves_unrelated_messages_untouched():
    msg = "post_scored post_id=post-1 score=85 decision=manual_review"
    assert _filtered_message(msg) == msg
