"""Core orchestration: search Threads -> filter/dedup -> ask DeepSeek ->
queue a draft reply for human review.

Nothing in this module ever calls threads_client.publish_reply() on its own.
Publishing only happens through publish_approved_reply(), which requires an
explicit human approval to already be recorded (approved=1) — set either via
the Telegram bot's "Опубликовать" button or `python -m app.main approve <id>`.
This is a deliberate design boundary, not a missing feature.
"""

import logging
import sqlite3
from datetime import datetime, timezone

from app.config import config, ALL_KEYWORDS
from app.db import now_iso, touch_daily_metric, touch_deepseek_usage
from app.dedup import author_in_cooldown, is_too_similar, post_too_old
from app.deepseek_client import DeepSeekClient
from app.lead_scoring import decide
from app.threads_client import ThreadsClient, ThreadsAPIError
from app.whatsapp import shorten_cta_if_needed

logger = logging.getLogger("pipeline")

# The project has dozens of phrases. Searching every phrase every hour wastes
# the Meta search quota and makes the worker look alive while every call is
# being rejected. Two phrases per cycle covers the full list in under a day
# while keeping a large safety margin for manual searches from the panel.
DEFAULT_KEYWORDS_PER_CYCLE = 2


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _scheduled_keywords(keywords: list[str]) -> list[str]:
    """Return a deterministic rotating keyword slice for the current worker
    interval. A restart in the same interval repeats the same tiny slice rather
    than unexpectedly consuming another full-list burst."""
    if not keywords:
        return []
    batch_size = min(DEFAULT_KEYWORDS_PER_CYCLE, len(keywords))
    interval_seconds = max(config.search_interval_minutes * 60, 60)
    slot = int(datetime.now(timezone.utc).timestamp() // interval_seconds)
    start = (slot * batch_size) % len(keywords)
    return [keywords[(start + offset) % len(keywords)] for offset in range(batch_size)]


def _recent_published_comments(conn: sqlite3.Connection, limit: int = 50) -> list[str]:
    rows = conn.execute(
        "SELECT generated_text FROM generated_replies WHERE published = 1 "
        "ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [r["generated_text"] for r in rows]


def _get_author(conn: sqlite3.Connection, author_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM authors WHERE author_id = ?", (author_id,)).fetchone()


def _upsert_author_seen(conn: sqlite3.Connection, author_id: str, username: str) -> None:
    conn.execute(
        "INSERT INTO authors (author_id, username, first_seen) VALUES (?, ?, ?) "
        "ON CONFLICT(author_id) DO NOTHING",
        (author_id, username, now_iso()),
    )


async def search_and_ingest(threads: ThreadsClient, deepseek: DeepSeekClient,
                             conn: sqlite3.Connection, keywords: list[str] | None = None) -> dict:
    """One search cycle. Automatic cycles rotate through a small keyword
    batch; explicitly supplied keywords (for example from the web panel) are
    searched exactly as requested."""
    active_keywords = _scheduled_keywords(ALL_KEYWORDS) if keywords is None else keywords
    summary = {
        "searched": 0,
        "found": 0,
        "queued": 0,
        "skipped": 0,
        "errors": 0,
        "keywords": active_keywords,
    }
    today = _today()
    logger.info("search_cycle_started keywords=%s", active_keywords)

    for keyword in active_keywords:
        summary["searched"] += 1
        touch_daily_metric(conn, today, searches=1)
        try:
            result = await threads.keyword_search(keyword)
        except ThreadsAPIError as exc:
            logger.exception("search_failed keyword=%s", keyword)
            touch_daily_metric(conn, today, errors=1)
            summary["errors"] += 1
            summary["last_error"] = {
                "keyword": keyword,
                "status_code": exc.status_code,
                "payload": exc.payload,
            }
            continue

        for raw_item in result.get("data", []):
            summary["found"] += 1
            touch_daily_metric(conn, today, posts_found=1)
            item = dict(raw_item)
            item["_matched_keyword"] = keyword
            try:
                outcome = await _process_post(threads, deepseek, conn, item, today)
            except Exception:
                # One malformed API item, database edge case, or downstream
                # model error must not kill the entire long-running worker.
                logger.exception("post_processing_failed post_id=%s keyword=%s",
                                 item.get("id"), keyword)
                touch_daily_metric(conn, today, errors=1)
                summary["errors"] += 1
                summary["skipped"] += 1
                continue
            if outcome == "queued":
                summary["queued"] += 1
            else:
                summary["skipped"] += 1

    logger.info("search_cycle_finished summary=%s", summary)
    return summary


async def _process_post(threads: ThreadsClient, deepseek: DeepSeekClient,
                         conn: sqlite3.Connection, item: dict, today: str) -> str:
    post_id = item.get("id")
    username = item.get("username", "")
    owner = item.get("owner") or {}
    owner_id = owner.get("id", "") if isinstance(owner, dict) else str(owner)
    author_id = owner_id or username
    text = item.get("text", "")
    permalink = item.get("permalink", "")
    published_at = item.get("timestamp") or now_iso()

    if not post_id or not text or not author_id:
        logger.info("post_skipped_missing_fields post_id=%s has_text=%s author_id=%s",
                    post_id, bool(text), bool(author_id))
        return "skipped"

    if conn.execute("SELECT 1 FROM threads_posts WHERE threads_post_id = ?", (post_id,)).fetchone():
        return "skipped"

    if author_id == config.threads_user_id:
        return "skipped"

    if post_too_old(published_at, config.post_max_age_hours):
        logger.info("post_skipped_too_old post_id=%s", post_id)
        return "skipped"

    author_row = _get_author(conn, author_id)
    if author_row and author_row["blocked"]:
        logger.info("post_skipped_author_blocked post_id=%s", post_id)
        return "skipped"
    if author_row and author_in_cooldown(author_row["last_contacted"], config.author_cooldown_days):
        logger.info("post_skipped_author_cooldown post_id=%s", post_id)
        return "skipped"

    _upsert_author_seen(conn, author_id, username)

    conn.execute(
        "INSERT INTO threads_posts (threads_post_id, author_id, author_username, text, "
        "permalink, found_keyword, published_at, found_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new')",
        (post_id, author_id, username, text, permalink,
         item.get("_matched_keyword", ""), published_at, now_iso()),
    )
    row_id = conn.execute("SELECT id FROM threads_posts WHERE threads_post_id = ?",
                           (post_id,)).fetchone()["id"]

    logger.info("post_sent_to_deepseek post_id=%s", post_id)
    analysis = await deepseek.analyze_post(text)
    touch_deepseek_usage(conn, today, deepseek.last_usage["prompt_tokens"],
                          deepseek.last_usage["completion_tokens"])
    logger.info("post_scored post_id=%s score=%s decision=%s",
                post_id, analysis["lead_score"], analysis["decision"])

    decision = decide(analysis["lead_score"], config.manual_review_min_score,
                       config.auto_publish_min_score)

    conn.execute(
        "UPDATE threads_posts SET language=?, problem_type=?, lead_score=?, relevance=?, "
        "decision=?, status=? WHERE id=?",
        (analysis["language"], analysis["problem_type"], analysis["lead_score"],
         str(analysis["relevant"]), decision,
         "analyzed" if decision == "skip" else "queued_for_review", row_id),
    )

    if not analysis["relevant"] or decision == "skip" or not analysis["comment"]:
        return "skipped"

    comment = shorten_cta_if_needed(analysis["comment"], analysis["problem_type"])
    recent = _recent_published_comments(conn)
    if is_too_similar(comment, recent):
        logger.info("comment_too_similar_retrying post_id=%s", post_id)
        analysis = await deepseek.analyze_post(text, avoid_comments=recent)
        comment = shorten_cta_if_needed(analysis["comment"], analysis["problem_type"])

    conn.execute(
        "INSERT INTO generated_replies (post_id, generated_text, model, prompt_version, "
        "approved, published, created_at) VALUES (?, ?, ?, ?, 0, 0, ?)",
        (row_id, comment, config.deepseek_model, "v1", now_iso()),
    )
    reply_row_id = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    touch_daily_metric(conn, today, relevant_posts=1, manual_reviews=1,
                        website_cta_count=1, whatsapp_cta_count=1)
    logger.info("reply_queued_for_review post_id=%s decision=%s", post_id, decision)

    try:
        from app.telegram_bot import notify_new_lead
        post_dict = dict(conn.execute("SELECT * FROM threads_posts WHERE id = ?", (row_id,)).fetchone())
        await notify_new_lead(post_dict, reply_row_id, comment)
    except Exception:
        logger.exception("telegram_notify_failed post_id=%s", post_id)

    return "queued"


async def publish_approved_reply(threads: ThreadsClient, conn: sqlite3.Connection,
                                  reply_id: int) -> dict:
    """Publish a reply that a human has already approved. Refuses to run if
    `approved` is not set, if DRY_RUN is on (logs the intent instead), or if
    the daily limit has been hit."""
    reply = conn.execute("SELECT * FROM generated_replies WHERE id = ?", (reply_id,)).fetchone()
    if reply is None:
        raise ValueError(f"no generated_replies row with id={reply_id}")
    if not reply["approved"]:
        raise PermissionError("reply is not approved — human confirmation required first")
    if reply["published"]:
        return {"status": "already_published", "threads_reply_id": reply["threads_reply_id"]}

    today = _today()
    published_today = conn.execute(
        "SELECT COUNT(*) c FROM generated_replies WHERE published = 1 "
        "AND date(published_at) = date('now')"
    ).fetchone()["c"]
    if published_today >= config.daily_reply_limit:
        logger.warning("daily_reply_limit_reached")
        return {"status": "daily_limit_reached"}

    post = conn.execute("SELECT * FROM threads_posts WHERE id = ?", (reply["post_id"],)).fetchone()

    if config.dry_run:
        logger.info("dry_run_would_publish reply_id=%s post_id=%s", reply_id, post["threads_post_id"])
        conn.execute(
            "UPDATE generated_replies SET published = 1, published_at = ?, "
            "threads_reply_id = 'DRY_RUN' WHERE id = ?", (now_iso(), reply_id)
        )
        return {"status": "dry_run_ok"}

    try:
        threads_reply_id = await threads.publish_reply(reply["generated_text"], post["threads_post_id"])
    except ThreadsAPIError as exc:
        logger.error("publish_failed reply_id=%s status=%s", reply_id, exc.status_code)
        conn.execute("UPDATE generated_replies SET error = ? WHERE id = ?",
                     (f"threads_api_error:{exc.status_code}", reply_id))
        touch_daily_metric(conn, today, errors=1)
        return {"status": "error", "error": str(exc.status_code), "payload": exc.payload}

    conn.execute(
        "UPDATE generated_replies SET published = 1, published_at = ?, threads_reply_id = ? "
        "WHERE id = ?", (now_iso(), threads_reply_id, reply_id)
    )
    conn.execute(
        "UPDATE authors SET last_contacted = ?, replies_count = replies_count + 1 "
        "WHERE author_id = ?", (now_iso(), post["author_id"])
    )
    touch_daily_metric(conn, today, replies_published=1)
    logger.info("reply_published reply_id=%s threads_reply_id=%s", reply_id, threads_reply_id)
    return {"status": "published", "threads_reply_id": threads_reply_id}
