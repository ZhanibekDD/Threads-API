from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.config import config
from app.content_generator import (
    THREADS_POST_MAX_CHARS,
    current_local_slot,
    is_publish_time_now,
    parse_publish_times,
    publish_next_approved,
)
from app.db import now_iso
from app.threads_client import ThreadsAPIError


def test_parse_publish_times_strips_whitespace():
    assert parse_publish_times("10:00, 18:00") == ["10:00", "18:00"]


def test_parse_publish_times_ignores_blanks():
    assert parse_publish_times("10:00,,18:00,") == ["10:00", "18:00"]


def test_parse_publish_times_empty_string():
    assert parse_publish_times("") == []


def test_current_local_slot_converts_utc_to_almaty():
    now = datetime(2026, 7, 18, 5, 0, tzinfo=timezone.utc)
    assert current_local_slot(now, "Asia/Almaty") == "10:00"


def test_current_local_slot_handles_naive_datetime_as_utc():
    now = datetime(2026, 7, 18, 13, 0)
    assert current_local_slot(now, "Asia/Almaty") == "18:00"


def test_is_publish_time_now_true_at_configured_slot():
    now = datetime(2026, 7, 18, 5, 0, tzinfo=timezone.utc)
    assert is_publish_time_now(["10:00", "18:00"], now, "Asia/Almaty") is True


def test_is_publish_time_now_false_outside_slots():
    now = datetime(2026, 7, 18, 6, 0, tzinfo=timezone.utc)
    assert is_publish_time_now(["10:00", "18:00"], now, "Asia/Almaty") is False


@pytest.mark.asyncio
async def test_publish_next_approved_skips_unapproved(conn):
    conn.execute(
        "INSERT INTO own_content (type, language, content, created_at, approved) "
        "VALUES ('post', 'ru', 'draft text', ?, 0)",
        (now_iso(),),
    )
    conn.commit()
    object.__setattr__(config, "auto_publish_own_content", True)
    threads = AsyncMock()

    result = await publish_next_approved(threads, conn)

    assert result["status"] == "nothing_to_publish"
    threads.create_own_post.assert_not_called()


@pytest.mark.asyncio
async def test_publish_next_approved_skips_non_post_types(conn):
    conn.execute(
        "INSERT INTO own_content (type, language, content, created_at, approved) "
        "VALUES ('story_idea', 'ru', 'idea text', ?, 1)",
        (now_iso(),),
    )
    conn.commit()
    object.__setattr__(config, "auto_publish_own_content", True)
    threads = AsyncMock()

    result = await publish_next_approved(threads, conn)

    assert result["status"] == "nothing_to_publish"
    threads.create_own_post.assert_not_called()


@pytest.mark.asyncio
async def test_publish_next_approved_skips_rejected_posts(conn):
    conn.execute(
        "INSERT INTO own_content "
        "(type, language, content, created_at, approved, rejected) "
        "VALUES ('post', 'ru', 'rejected text', ?, 1, 1)",
        (now_iso(),),
    )
    conn.commit()
    object.__setattr__(config, "auto_publish_own_content", True)
    threads = AsyncMock()

    result = await publish_next_approved(threads, conn)

    assert result["status"] == "nothing_to_publish"
    threads.create_own_post.assert_not_called()


@pytest.mark.asyncio
async def test_publish_next_approved_respects_master_switch(conn):
    conn.execute(
        "INSERT INTO own_content (type, language, content, created_at, approved) "
        "VALUES ('post', 'ru', 'approved post', ?, 1)",
        (now_iso(),),
    )
    conn.commit()
    object.__setattr__(config, "auto_publish_own_content", False)
    threads = AsyncMock()

    result = await publish_next_approved(threads, conn)

    assert result["status"] == "disabled"
    threads.create_own_post.assert_not_called()


@pytest.mark.asyncio
async def test_publish_next_approved_publishes_and_marks_row(conn):
    conn.execute(
        "INSERT INTO own_content (type, language, content, created_at, approved) "
        "VALUES ('post', 'ru', 'approved post', ?, 1)",
        (now_iso(),),
    )
    conn.commit()
    row_id = conn.execute("SELECT id FROM own_content").fetchone()["id"]

    object.__setattr__(config, "auto_publish_own_content", True)
    threads = AsyncMock()
    threads.create_own_post.return_value = "threads_post_123"

    result = await publish_next_approved(threads, conn)

    assert result == {
        "status": "published",
        "id": row_id,
        "threads_post_id": "threads_post_123",
    }
    row = conn.execute(
        "SELECT * FROM own_content WHERE id = ?",
        (row_id,),
    ).fetchone()
    assert row["published"] == 1
    assert row["threads_post_id"] == "threads_post_123"
    assert row["published_at"] is not None


@pytest.mark.asyncio
async def test_publish_next_approved_rejects_oversized_content_without_api(conn):
    long_text = "a" * (THREADS_POST_MAX_CHARS + 1)
    conn.execute(
        "INSERT INTO own_content (type, language, content, created_at, approved) "
        "VALUES ('post', 'ru', ?, ?, 1)",
        (long_text, now_iso()),
    )
    conn.commit()
    row_id = conn.execute("SELECT id FROM own_content").fetchone()["id"]

    object.__setattr__(config, "auto_publish_own_content", True)
    threads = AsyncMock()

    result = await publish_next_approved(threads, conn)

    assert result["status"] == "content_too_long"
    assert result["id"] == row_id
    threads.create_own_post.assert_not_called()
    row = conn.execute(
        "SELECT approved, error FROM own_content WHERE id = ?",
        (row_id,),
    ).fetchone()
    assert row["approved"] == 0
    assert "500" in row["error"]


@pytest.mark.asyncio
async def test_oversized_item_does_not_block_next_valid_post(conn):
    long_text = "a" * (THREADS_POST_MAX_CHARS + 1)
    conn.execute(
        "INSERT INTO own_content (type, language, content, created_at, approved) "
        "VALUES ('post', 'ru', ?, ?, 1)",
        (long_text, now_iso()),
    )
    conn.execute(
        "INSERT INTO own_content (type, language, content, created_at, approved) "
        "VALUES ('post', 'ru', 'short valid post', ?, 1)",
        (now_iso(),),
    )
    conn.commit()

    object.__setattr__(config, "auto_publish_own_content", True)
    threads = AsyncMock()
    threads.create_own_post.return_value = "threads_id_ok"

    first = await publish_next_approved(threads, conn)
    second = await publish_next_approved(threads, conn)

    assert first["status"] == "content_too_long"
    assert second["status"] == "published"
    threads.create_own_post.assert_called_once_with("short valid post")


@pytest.mark.asyncio
async def test_publish_next_approved_handles_api_error_without_raising(conn):
    conn.execute(
        "INSERT INTO own_content (type, language, content, created_at, approved) "
        "VALUES ('post', 'ru', 'approved post', ?, 1)",
        (now_iso(),),
    )
    conn.commit()
    row_id = conn.execute("SELECT id FROM own_content").fetchone()["id"]

    object.__setattr__(config, "auto_publish_own_content", True)
    threads = AsyncMock()
    threads.create_own_post.side_effect = ThreadsAPIError(
        500,
        {"error": "boom"},
    )

    result = await publish_next_approved(threads, conn)

    assert result["status"] == "error"
    row = conn.execute(
        "SELECT approved, published, error FROM own_content WHERE id = ?",
        (row_id,),
    ).fetchone()
    assert row["approved"] == 0
    assert row["published"] == 0
    assert row["error"] is not None


@pytest.mark.asyncio
async def test_publish_next_approved_picks_oldest_first(conn):
    for text in ("first", "second"):
        conn.execute(
            "INSERT INTO own_content "
            "(type, language, content, created_at, approved) "
            "VALUES ('post', 'ru', ?, ?, 1)",
            (text, now_iso()),
        )
    conn.commit()
    object.__setattr__(config, "auto_publish_own_content", True)
    threads = AsyncMock()
    threads.create_own_post.return_value = "id1"

    result = await publish_next_approved(threads, conn)

    threads.create_own_post.assert_called_once_with("first")
    assert result["status"] == "published"
