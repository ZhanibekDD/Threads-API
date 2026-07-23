from unittest.mock import AsyncMock

import pytest

from app.config import config
from app.db import now_iso
from app.pipeline import publish_approved_reply


def _insert_post_and_reply(conn, approved=0):
    conn.execute(
        "INSERT INTO threads_posts (threads_post_id, author_id, author_username, text, "
        "permalink, found_at, status) VALUES ('p1', 'author1', 'user1', 'text', 'link', ?, 'queued_for_review')",
        (now_iso(),),
    )
    post_id = conn.execute("SELECT id FROM threads_posts WHERE threads_post_id='p1'").fetchone()["id"]
    conn.execute(
        "INSERT INTO generated_replies (post_id, generated_text, model, prompt_version, "
        "approved, published, created_at) VALUES (?, 'draft comment', 'model', 'v1', ?, 0, ?)",
        (post_id, approved, now_iso()),
    )
    conn.commit()
    return conn.execute("SELECT id FROM generated_replies WHERE post_id=?", (post_id,)).fetchone()["id"]


@pytest.mark.asyncio
async def test_publish_requires_human_approval(conn):
    reply_id = _insert_post_and_reply(conn, approved=0)
    with pytest.raises(PermissionError):
        await publish_approved_reply(threads=AsyncMock(), conn=conn, reply_id=reply_id)


@pytest.mark.asyncio
async def test_dry_run_never_calls_threads_api_or_marks_published(conn):
    reply_id = _insert_post_and_reply(conn, approved=1)
    object.__setattr__(config, "dry_run", True)
    threads = AsyncMock()
    result = await publish_approved_reply(threads=threads, conn=conn, reply_id=reply_id)
    assert result["status"] == "dry_run_ok"
    threads.publish_reply.assert_not_called()
    row = conn.execute("SELECT * FROM generated_replies WHERE id=?", (reply_id,)).fetchone()
    assert row["approved"] == 1
    assert row["published"] == 0
    assert row["published_at"] is None
    assert row["threads_reply_id"] is None


@pytest.mark.asyncio
async def test_daily_limit_blocks_publish(conn):
    reply_id = _insert_post_and_reply(conn, approved=1)
    object.__setattr__(config, "dry_run", False)
    object.__setattr__(config, "daily_reply_limit", 0)
    threads = AsyncMock()
    try:
        result = await publish_approved_reply(threads=threads, conn=conn, reply_id=reply_id)
        assert result["status"] == "daily_limit_reached"
        threads.publish_reply.assert_not_called()
    finally:
        object.__setattr__(config, "daily_reply_limit", 20)
        object.__setattr__(config, "dry_run", True)


@pytest.mark.asyncio
async def test_successful_publish_updates_author_and_reply(conn):
    reply_id = _insert_post_and_reply(conn, approved=1)
    conn.execute(
        "INSERT INTO authors (author_id, username, first_seen, replies_count) "
        "VALUES ('author1', 'user1', ?, 0)", (now_iso(),)
    )
    conn.commit()

    object.__setattr__(config, "dry_run", False)
    threads = AsyncMock()
    threads.publish_reply.return_value = "threads_reply_999"
    try:
        result = await publish_approved_reply(threads=threads, conn=conn, reply_id=reply_id)
    finally:
        object.__setattr__(config, "dry_run", True)

    assert result["status"] == "published"
    assert result["threads_reply_id"] == "threads_reply_999"
    author = conn.execute("SELECT * FROM authors WHERE author_id='author1'").fetchone()
    assert author["replies_count"] == 1
    assert author["last_contacted"] is not None


@pytest.mark.asyncio
async def test_already_published_is_idempotent(conn):
    reply_id = _insert_post_and_reply(conn, approved=1)
    conn.execute(
        "UPDATE generated_replies SET published=1, threads_reply_id='old_id' WHERE id=?",
        (reply_id,),
    )
    conn.commit()
    threads = AsyncMock()
    result = await publish_approved_reply(threads=threads, conn=conn, reply_id=reply_id)
    assert result["status"] == "already_published"
    threads.publish_reply.assert_not_called()
