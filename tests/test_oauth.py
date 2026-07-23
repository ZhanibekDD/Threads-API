from datetime import datetime, timedelta, timezone

from app import oauth


def test_generate_state_is_random_and_stored(conn):
    s1 = oauth.generate_state(conn)
    s2 = oauth.generate_state(conn)
    assert s1 != s2
    assert len(s1) > 20
    rows = conn.execute("SELECT state FROM oauth_states").fetchall()
    assert {r["state"] for r in rows} == {s1, s2}


def test_consume_state_accepts_valid_unexpired_state(conn):
    state = oauth.generate_state(conn)
    assert oauth.consume_state(conn, state) is True


def test_consume_state_is_one_time_use(conn):
    state = oauth.generate_state(conn)
    assert oauth.consume_state(conn, state) is True
    assert oauth.consume_state(conn, state) is False  # replay must fail


def test_consume_state_rejects_unknown_state(conn):
    assert oauth.consume_state(conn, "never-issued-state") is False


def test_consume_state_rejects_missing_state(conn):
    assert oauth.consume_state(conn, None) is False
    assert oauth.consume_state(conn, "") is False


def test_consume_state_rejects_expired_state(conn):
    state = "expired-state-123"
    old_time = (datetime.now(timezone.utc) - timedelta(minutes=oauth.STATE_TTL_MINUTES + 1)).isoformat()
    conn.execute("INSERT INTO oauth_states (state, created_at) VALUES (?, ?)", (state, old_time))
    conn.commit()
    assert oauth.consume_state(conn, state) is False


def test_build_authorize_url_contains_state_and_scopes():
    url = oauth.build_authorize_url("mystate123", scopes=["threads_basic", "threads_keyword_search"])
    assert "state=mystate123" in url
    assert "threads_basic" in url
    assert "threads_keyword_search" in url
    assert url.startswith(oauth.AUTHORIZE_BASE)


def test_scopes_for_stage_1_excludes_manage_replies():
    scopes = oauth.scopes_for_stage(1)
    assert "threads_manage_replies" not in scopes
    assert set(scopes) == {"threads_basic", "threads_keyword_search", "threads_content_publish"}


def test_scopes_for_stage_2_includes_manage_replies():
    scopes = oauth.scopes_for_stage(2)
    assert "threads_manage_replies" in scopes


def test_save_and_get_active_token_prefers_db_over_env(conn):
    assert oauth.get_token_row(conn) is None
    oauth.save_token(conn, "db-token-abc", 3600, "uid1", "someuser")
    assert oauth.get_active_access_token(conn) == "db-token-abc"


def test_connection_status_reports_disconnected_when_no_token(conn):
    status = oauth.connection_status(conn)
    assert status == {"connected": False}


def test_connection_status_reports_connected_with_username(conn):
    oauth.save_token(conn, "tok", 3600, "uid1", "zakonexpert.kzz")
    status = oauth.connection_status(conn)
    assert status["connected"] is True
    assert status["threads_username"] == "zakonexpert.kzz"
    assert "tok" not in str(status)  # raw token must never appear in the status payload


def test_connection_status_flags_expiring_soon(conn):
    oauth.save_token(conn, "tok", expires_in_seconds=3600, threads_user_id="uid1", threads_username="u")
    status = oauth.connection_status(conn)
    assert status["expires_soon"] is True
    assert status["expired"] is False


def test_connection_status_flags_expired(conn):
    oauth.save_token(conn, "tok", expires_in_seconds=-10, threads_user_id="uid1", threads_username="u")
    status = oauth.connection_status(conn)
    assert status["expired"] is True
