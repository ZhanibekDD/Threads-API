"""Threads OAuth connect/callback flow for a single internal account.

Not a multi-user system: there is exactly one row in oauth_tokens (id=1),
matching "this is ZakonExpert's own account, not a public service" — see
docs/META_APP_REVIEW.md.
"""

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from app.config import config
from app.db import now_iso

AUTHORIZE_BASE = "https://threads.net/oauth/authorize"
STATE_TTL_MINUTES = 10

# Stage 1: minimum permissions to find and review leads, and to publish the
# business's own content. Stage 2 adds threads_manage_replies, which is the
# specific permission required to actually post a reply to someone else's
# post (see docs/META_APP_REVIEW.md for the Meta-documentation justification).
STAGE_1_SCOPES = ["threads_basic", "threads_keyword_search", "threads_content_publish"]
STAGE_2_SCOPES = STAGE_1_SCOPES + ["threads_manage_replies"]


def scopes_for_stage(stage: int | None = None) -> list[str]:
    stage = stage if stage is not None else config.oauth_stage
    return STAGE_2_SCOPES if stage >= 2 else STAGE_1_SCOPES


def generate_state(conn: sqlite3.Connection) -> str:
    state = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO oauth_states (state, created_at) VALUES (?, ?)", (state, now_iso()))
    conn.commit()
    return state


def consume_state(conn: sqlite3.Connection, state: str | None) -> bool:
    """One-time-use CSRF check: looks up the state, deletes it immediately
    regardless of outcome (so a state can never be replayed), and returns
    whether it was valid and not expired."""
    if not state:
        return False
    row = conn.execute("SELECT created_at FROM oauth_states WHERE state = ?", (state,)).fetchone()
    conn.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
    conn.commit()
    if row is None:
        return False
    created_at = datetime.fromisoformat(row["created_at"])
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created_at <= timedelta(minutes=STATE_TTL_MINUTES)


def build_authorize_url(state: str, scopes: list[str] | None = None) -> str:
    scopes = scopes or scopes_for_stage()
    params = {
        "client_id": config.threads_app_id,
        "redirect_uri": config.threads_redirect_uri,
        "scope": ",".join(scopes),
        "response_type": "code",
        "state": state,
    }
    return f"{AUTHORIZE_BASE}?{urlencode(params)}"


def save_token(conn: sqlite3.Connection, access_token: str, expires_in_seconds: int,
               threads_user_id: str, threads_username: str) -> None:
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)).isoformat()
    ts = now_iso()
    conn.execute(
        "INSERT INTO oauth_tokens (id, access_token, obtained_at, expires_at, "
        "threads_user_id, threads_username, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET access_token=excluded.access_token, "
        "expires_at=excluded.expires_at, threads_user_id=excluded.threads_user_id, "
        "threads_username=excluded.threads_username, updated_at=excluded.updated_at",
        (access_token, ts, expires_at, threads_user_id, threads_username, ts),
    )
    conn.commit()


def get_token_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM oauth_tokens WHERE id = 1").fetchone()


def get_active_access_token(conn: sqlite3.Connection) -> str:
    """DB-stored OAuth token (from the /threads/connect flow) takes priority.
    Falls back to the THREADS_ACCESS_TOKEN env var for manual/CLI bootstrap
    before the first OAuth connection has been made."""
    row = get_token_row(conn)
    if row is not None and row["access_token"]:
        return row["access_token"]
    return config.threads_access_token


def connection_status(conn: sqlite3.Connection) -> dict:
    """Safe to render directly in the UI — never includes the raw token."""
    row = get_token_row(conn)
    if row is None:
        return {"connected": False}
    expires_at = datetime.fromisoformat(row["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return {
        "connected": True,
        "threads_username": row["threads_username"],
        "threads_user_id": row["threads_user_id"],
        "expires_at": row["expires_at"],
        "expires_soon": expires_at - datetime.now(timezone.utc) < timedelta(days=7),
        "expired": expires_at <= datetime.now(timezone.utc),
    }


def _base64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def parse_signed_request(signed_request: str, app_secret: str) -> dict | None:
    """Verifies and decodes Meta's `signed_request` format, used for the
    Deauthorize Callback and Data Deletion Request Callback webhooks.
    Returns None if the request is missing, malformed, or the signature
    doesn't match (i.e. it wasn't actually sent by Meta with our app secret).
    """
    if not signed_request or "." not in signed_request:
        return None
    encoded_sig, payload = signed_request.split(".", 1)
    try:
        sig = _base64url_decode(encoded_sig)
        data_bytes = _base64url_decode(payload)
    except Exception:
        return None

    expected_sig = hmac.new(app_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected_sig):
        return None

    try:
        data = json.loads(data_bytes)
    except json.JSONDecodeError:
        return None

    if data.get("algorithm") not in ("HMAC-SHA256", None):
        return None
    return data


async def refresh_if_needed(conn: sqlite3.Connection, days_before_expiry: int = 5) -> bool:
    """Refreshes the stored long-lived token if it's within
    `days_before_expiry` of expiring. Returns True if a refresh happened."""
    from app.threads_client import ThreadsClient  # local import: avoid import cycle at module load

    row = get_token_row(conn)
    if row is None:
        return False
    expires_at = datetime.fromisoformat(row["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at - datetime.now(timezone.utc) > timedelta(days=days_before_expiry):
        return False

    client = ThreadsClient(access_token=row["access_token"])
    try:
        refreshed = await client.refresh_long_lived_token()
    finally:
        await client.aclose()

    save_token(conn, refreshed["access_token"], refreshed.get("expires_in", 60 * 24 * 3600),
               row["threads_user_id"], row["threads_username"])
    return True
