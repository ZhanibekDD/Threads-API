from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.config import config
from app.db import now_iso
from app.pipeline import _process_post


def _item(post_id="post-1", author="author-1", text="Kaspi арестовали счет, что делать?"):
    return {
        "id": post_id,
        "username": author,
        "text": text,
        "permalink": f"https://threads.net/{post_id}",
        "timestamp": now_iso(),
    }


def _deepseek(score, decision, relevant=True, comment="Проверьте арест. zakonexpertt.kz +7 775 299-87-38"):
    ds = AsyncMock()
    ds.analyze_post.return_value = {
        "relevant": relevant,
        "decision": decision,
        "language": "ru",
        "problem_type": "account_arrest",
        "lead_score": score,
        "urgency": "high",
        "kazakhstan_probability": 90,
        "risk_flags": [],
        "comment": comment,
        "reason": "test",
    }
    ds.last_usage = {"prompt_tokens": 10, "completion_tokens": 5}
    return ds


@pytest.mark.asyncio
async def test_low_score_post_is_skipped_not_queued(conn):
    threads = AsyncMock()
    deepseek = _deepseek(score=20, decision="skip", relevant=False, comment="")
    outcome = await _process_post(threads, deepseek, conn, _item(), datetime.now(timezone.utc).date().isoformat())
    assert outcome == "skipped"
    post = conn.execute("SELECT * FROM threads_posts WHERE threads_post_id='post-1'").fetchone()
    assert post["status"] == "analyzed"
    assert conn.execute("SELECT COUNT(*) c FROM generated_replies").fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_high_score_post_is_queued_for_review(conn):
    threads = AsyncMock()
    deepseek = _deepseek(score=85, decision="manual_review")
    outcome = await _process_post(threads, deepseek, conn, _item(), datetime.now(timezone.utc).date().isoformat())
    assert outcome == "queued"
    post = conn.execute("SELECT * FROM threads_posts WHERE threads_post_id='post-1'").fetchone()
    assert post["status"] == "queued_for_review"
    reply = conn.execute("SELECT * FROM generated_replies WHERE post_id=?", (post["id"],)).fetchone()
    assert reply["approved"] == 0
    assert reply["published"] == 0


@pytest.mark.asyncio
async def test_duplicate_post_id_is_skipped(conn):
    threads = AsyncMock()
    deepseek = _deepseek(score=85, decision="manual_review")
    today = datetime.now(timezone.utc).date().isoformat()
    first = await _process_post(threads, deepseek, conn, _item(), today)
    assert first == "queued"
    deepseek2 = _deepseek(score=85, decision="manual_review")
    second = await _process_post(threads, deepseek2, conn, _item(), today)
    assert second == "skipped"
    deepseek2.analyze_post.assert_not_called()


@pytest.mark.asyncio
async def test_author_in_cooldown_is_skipped_before_deepseek_call(conn):
    conn.execute(
        "INSERT INTO authors (author_id, username, first_seen, last_contacted, blocked) "
        "VALUES ('author-1', 'author-1', ?, ?, 0)", (now_iso(), now_iso())
    )
    conn.commit()
    threads = AsyncMock()
    deepseek = _deepseek(score=85, decision="manual_review")
    outcome = await _process_post(threads, deepseek, conn, _item(post_id="post-2"),
                                   datetime.now(timezone.utc).date().isoformat())
    assert outcome == "skipped"
    deepseek.analyze_post.assert_not_called()


@pytest.mark.asyncio
async def test_blocked_author_is_skipped(conn):
    conn.execute(
        "INSERT INTO authors (author_id, username, first_seen, blocked) "
        "VALUES ('author-1', 'author-1', ?, 1)", (now_iso(),)
    )
    conn.commit()
    threads = AsyncMock()
    deepseek = _deepseek(score=85, decision="manual_review")
    outcome = await _process_post(threads, deepseek, conn, _item(post_id="post-3"),
                                   datetime.now(timezone.utc).date().isoformat())
    assert outcome == "skipped"
    deepseek.analyze_post.assert_not_called()


@pytest.mark.asyncio
async def test_too_old_post_is_skipped_before_deepseek_call(conn):
    from datetime import timedelta
    old_item = _item(post_id="post-4")
    old_item["timestamp"] = (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat()
    threads = AsyncMock()
    deepseek = _deepseek(score=85, decision="manual_review")
    object.__setattr__(config, "post_max_age_hours", 12)
    outcome = await _process_post(threads, deepseek, conn, old_item,
                                   datetime.now(timezone.utc).date().isoformat())
    assert outcome == "skipped"
    deepseek.analyze_post.assert_not_called()
