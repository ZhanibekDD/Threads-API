import logging
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.config import config
from app.threads_client import ThreadsClient

FAKE_SHORT_TOKEN = "SHORT_LIVED_TEST_TOKEN_abc123"
FAKE_LONG_TOKEN = "LONG_LIVED_TEST_TOKEN_xyz789do_not_leak"
TEST_APP_ID = "test-app-id"
TEST_APP_SECRET = "test-app-secret"
TEST_REDIRECT_URI = "http://127.0.0.1:5000/threads/callback"


@pytest.fixture()
def web_client():
    """Use an isolated DB and explicit OAuth settings, independent of .env."""
    tmp_dir = tempfile.mkdtemp()
    tmp_db = str(Path(tmp_dir) / "test_web.db")
    originals = {
        "database_path": config.database_path,
        "threads_app_id": config.threads_app_id,
        "threads_app_secret": config.threads_app_secret,
        "threads_redirect_uri": config.threads_redirect_uri,
        "review_panel_password": config.review_panel_password,
    }
    object.__setattr__(config, "database_path", tmp_db)
    object.__setattr__(config, "threads_app_id", TEST_APP_ID)
    object.__setattr__(config, "threads_app_secret", TEST_APP_SECRET)
    object.__setattr__(config, "threads_redirect_uri", TEST_REDIRECT_URI)
    object.__setattr__(config, "review_panel_password", "")
    try:
        import app.web as web

        web.app.testing = True
        yield web.app.test_client()
    finally:
        for name, value in originals.items():
            object.__setattr__(config, name, value)


def _mocked_exchange():
    return (
        patch.object(
            ThreadsClient,
            "exchange_code_for_token",
            AsyncMock(
                return_value={
                    "access_token": FAKE_SHORT_TOKEN,
                    "user_id": "27910545581903383",
                }
            ),
        ),
        patch.object(
            ThreadsClient,
            "exchange_for_long_lived_token",
            AsyncMock(
                return_value={
                    "access_token": FAKE_LONG_TOKEN,
                    "expires_in": 5183944,
                }
            ),
        ),
        patch.object(
            ThreadsClient,
            "get_me",
            AsyncMock(
                return_value={
                    "id": "27910545581903383",
                    "username": "zakonexpert.kzz",
                }
            ),
        ),
    )


def test_dashboard_shows_disconnected_by_default(web_client):
    response = web_client.get("/")
    assert response.status_code == 200
    assert "не подключён".encode("utf-8") in response.data


def test_connect_redirects_to_meta_authorize_url_with_state(web_client):
    response = web_client.get("/threads/connect")
    assert response.status_code == 302
    assert response.headers["Location"].startswith(
        "https://threads.net/oauth/authorize"
    )
    assert "state=" in response.headers["Location"]


def test_connect_without_app_id_redirects_home_with_error(web_client):
    original = config.threads_app_id
    object.__setattr__(config, "threads_app_id", "")
    try:
        response = web_client.get("/threads/connect", follow_redirects=True)
        assert response.status_code == 200
        assert "threads/callback" not in response.request.path
    finally:
        object.__setattr__(config, "threads_app_id", original)


def test_callback_with_invalid_state_is_rejected(web_client):
    response = web_client.get(
        "/threads/callback?code=whatever&state=never-issued"
    )
    assert response.status_code == 400


def test_full_oauth_roundtrip_saves_token_and_never_leaks_it(
    web_client,
    caplog,
):
    connect_response = web_client.get("/threads/connect")
    location = connect_response.headers["Location"]
    state = location.split("state=")[1].split("&")[0]

    first_mock, second_mock, third_mock = _mocked_exchange()
    with first_mock, second_mock, third_mock:
        with caplog.at_level(logging.DEBUG):
            callback_response = web_client.get(
                f"/threads/callback?code=fake-auth-code&state={state}"
            )

    assert callback_response.status_code == 302
    assert callback_response.headers["Location"].endswith("/")
    assert FAKE_LONG_TOKEN not in callback_response.headers["Location"]

    full_log = "\n".join(record.getMessage() for record in caplog.records)
    assert FAKE_LONG_TOKEN not in full_log
    assert FAKE_SHORT_TOKEN not in full_log

    dashboard_response = web_client.get("/")
    assert FAKE_LONG_TOKEN.encode() not in dashboard_response.data
    assert b"zakonexpert.kzz" in dashboard_response.data

    from app import oauth
    from app.db import get_connection

    conn = get_connection(config.database_path)
    row = oauth.get_token_row(conn)
    assert row["access_token"] == FAKE_LONG_TOKEN
    conn.close()


def test_replayed_state_after_successful_callback_is_rejected(web_client):
    connect_response = web_client.get("/threads/connect")
    state = connect_response.headers["Location"].split("state=")[1].split("&")[0]

    first_mock, second_mock, third_mock = _mocked_exchange()
    with first_mock, second_mock, third_mock:
        first = web_client.get(
            f"/threads/callback?code=fake-code&state={state}"
        )
    assert first.status_code == 302

    second = web_client.get(
        f"/threads/callback?code=fake-code&state={state}"
    )
    assert second.status_code == 400


def _sign(payload: dict) -> str:
    import base64
    import hashlib
    import hmac
    import json

    payload_json = json.dumps(payload).encode("utf-8")
    encoded_payload = base64.urlsafe_b64encode(payload_json).rstrip(b"=").decode(
        "utf-8"
    )
    signature = hmac.new(
        config.threads_app_secret.encode("utf-8"),
        encoded_payload.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode(
        "utf-8"
    )
    return f"{encoded_signature}.{encoded_payload}"


def test_deauthorize_webhook_clears_stored_token(web_client):
    from app import oauth
    from app.db import get_connection, init_db

    conn = get_connection(config.database_path)
    init_db(conn)
    oauth.save_token(
        conn,
        "some-token",
        3600,
        "27910545581903383",
        "zakonexpert.kzz",
    )
    conn.close()

    signed = _sign(
        {"algorithm": "HMAC-SHA256", "user_id": "27910545581903383"}
    )
    response = web_client.post(
        "/threads/deauthorize",
        data={"signed_request": signed},
    )
    assert response.status_code == 200

    conn = get_connection(config.database_path)
    assert oauth.get_token_row(conn) is None
    conn.close()


def test_deauthorize_webhook_rejects_bad_signature(web_client):
    response = web_client.post(
        "/threads/deauthorize",
        data={"signed_request": "garbage.garbage"},
    )
    assert response.status_code == 400


def test_data_deletion_webhook_returns_confirmation(web_client):
    signed = _sign(
        {"algorithm": "HMAC-SHA256", "user_id": "27910545581903383"}
    )
    response = web_client.post(
        "/threads/data-deletion",
        data={"signed_request": signed},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert "confirmation_code" in body
    assert "url" in body
    assert body["confirmation_code"] in body["url"]


def test_data_deletion_webhook_rejects_bad_signature(web_client):
    response = web_client.post(
        "/threads/data-deletion",
        data={"signed_request": "garbage.garbage"},
    )
    assert response.status_code == 400


def test_data_deletion_status_page_shows_code(web_client):
    response = web_client.get("/threads/data-deletion-status?id=abc123")
    assert response.status_code == 200
    assert b"abc123" in response.data


def test_webhooks_bypass_basic_auth(web_client):
    original = config.review_panel_password
    object.__setattr__(config, "review_panel_password", "some-password")
    try:
        response = web_client.post(
            "/threads/deauthorize",
            data={"signed_request": "garbage.garbage"},
        )
        assert response.status_code == 400
    finally:
        object.__setattr__(config, "review_panel_password", original)
