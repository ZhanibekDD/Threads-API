"""Search Threads, qualify leads, and queue draft replies for human review."""

import logging
import math
import sqlite3
from datetime import datetime, timezone

from app.config import ALL_KEYWORDS, config
from app.db import now_iso, touch_daily_metric, touch_deepseek_usage
from app.dedup import author_in_cooldown, is_too_similar, post_too_old
from app.deepseek_client import DeepSeekClient
from app.lead_scoring import decide
from app.threads_client import ThreadsAPIError, ThreadsClient
from app.whatsapp import shorten_cta_if_needed

logger = logging.getLogger("pipeline")

MIN_KEYWORDS_PER_CYCLE = 2


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _keywords_per_cycle(keyword_count: int) -> int:
    """Cover every phrase before a post exceeds the freshness window."""
    if keyword_count <= 0:
        return 0
    interval_hours = max(config.search_interval_minutes, 1) / 60
    freshness_hours = max(config.post_max_age_hours, 1)
    required = math.ceil(keyword_count * interval_hours / freshness_hours)
    return min(
        keyword_count,
        max(MIN_KEYWORDS_PER_CYCLE, required + 1),
    )


def _scheduled_keywords(keywords: list[str]) -> list[str]:
    """Return a deterministic rotating slice for the current worker slot."""
    if not keywords:
        return []
    batch_size = _keywords_per_cycle(len(keywords))
    interval_seconds = max(config.search_interval_minutes * 60, 60)
    slot = int(datetime.now(timezone.utc).timestamp() // interval_seconds)
    start = (slot * batch_size) % len(keywords)
    return [
        keywords[(start + offset) % len(keywords)]
        for offset in range(batch_size)
    ]


def _recent_published_comments(
    conn: sqlite3.Connection,
    limit: int = 50,
) -> list[str]:
    rows = conn.execute(
        "SELECT generated_text FROM generated_replies WHERE published = 1 "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [row["generated_text"] for row in rows]


def _get_author(
    conn: sqlite3.Connection,
    author_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM authors WHERE author_id = ?",
        (author_id,),
    ).fetchone()


def _upsert_author_seen(
    conn: sqlite3.Connection,
    author_id: str,
    username: str,
) -> None:
    conn.execute(
        "INSERT INTO authors (author_id, username, first_seen) "
        "VALUES (?, ?, ?) ON CONFLICT(author_id) DO NOTHING",
        (author_id, username, now_iso()),
    )


async def search_and_ingest(
    threads: ThreadsClient,
    deepseek: DeepSeekClient,
    conn: sqlite3.Connection,
    keywords: list[str] | None = None,
) -> dict:
    """Run one search cycle and return an observable summary."""
    active_keywords = (
        _scheduled_keywords(ALL_KEYWORDS)
        if keywords is None
        else keywords
    )
    summary: dict = {
        "searched": 0,
        "found": 0,
        "queued": 0,
        "skipped": 0,
        "errors": 0,
        "keywords": active_keywords,
    }
    today = _today()
    logger.info("search_cycle_started keywords=%s", active_keywords)

    own_user_id = config.threads_user_id
    if not own_user_id and isinstance(threads, ThreadsClient):
        try:
            own_user_id = await threads._get_user_id()
        except ThreadsAPIError as exc:
            logger.warning(
                "own_user_id_resolution_failed status=%s",
                exc.status_code,
            )
            summary["own_user_id_error"] = {
                "status_code": exc.status_code,
                "payload": exc.payload,
            }

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
            try:
                item = dict(raw_item or {})
                item["_matched_keyword"] = keyword
                outcome = await _process_post(
                    threads,
                    deepseek,
                    conn,
                    item,
                    today,
                    own_user_id=own_user_id,
                )
            except Exception:
                post_id = (
                    raw_item.get("id")
                    if isinstance(raw_item, dict)
                    else None
                )
                logger.exception(
                    "post_processing_failed post_id=%s keyword=%s",
                    post_id,
                    keyword,
                )
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


async def _process_post(
    threads: ThreadsClient,
    deepseek: DeepSeekClient,
    conn: sqlite3.Connection,
    item: dict,
    today: str,
    own_user_id: str | None = None,
) -> str:
    post_id = item.get("id")
    username = item.get("username", "")
    owner = item.get("owner") or {}
    owner_id = (
        owner.get("id", "")
        if isinstance(owner, dict)
        else str(owner)
    )
    author_id = owner_id or username
    text = item.get("text", "")
    permalink = item.get("permalink", "")
    published_at = item.get("timestamp") or now_iso()

    if not post_id or not text or not author_id:
        logger.info(
            "post_skipped_missing_fields post_id=%s has_text=%s author_id=%s",
            post_id,
            bool(text),
            bool(author_id),
        )
        return "skipped"

    existing = conn.execute(
        "SELECT 1 FROM threads_posts WHERE threads_post_id = ?",
        (post_id,),
    ).fetchone()
    if existing:
        return "skipped"

    if own_user_id and author_id == own_user_id:
        logger.info("post_skipped_own_account post_id=%s", post_id)
        return "skipped"

    if post_too_old(published_at, config.post_max_age_hours):
        logger.info("post_skipped_too_old post_id=%s", post_id)
        return "skipped"

    author_row = _get_author(conn, author_id)
    if author_row and author_row["blocked"]:
        logger.info("post_skipped_author_blocked post_id=%s", post_id)
        return "skipped"
    if author_row and author_in_cooldown(
        author_row["last_contacted"],
        config.author_cooldown_days,
    ):
        logger.info("post_skipped_author_cooldown post_id=%s", post_id)
        return "skipped"

    _upsert_author_seen(conn, author_id, username)
    conn.execute(
        "INSERT INTO threads_posts "
        "(threads_post_id, author_id, author_username, text, permalink, "
        "found_keyword, published_at, found_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new')",
        (
            post_id,
            author_id,
            username,
            text,
            permalink,
            item.get("_matched_keyword", ""),
            published_at,
            now_iso(),
        ),
    )
    row_id = conn.execute(
        "SELECT id FROM threads_posts WHERE threads_post_id = ?",
        (post_id,),
    ).fetchone()["id"]

    logger.info("post_sent_to_deepseek post_id=%s", post_id)
    analysis = await deepseek.analyze_post(text)
    touch_deepseek_usage(
        conn,
        today,
        deepseek.last_usage["prompt_tokens"],
        deepseek.last_usage["completion_tokens"],
    )
    decision = decide(
        analysis["lead_score"],
        config.manual_review_min_score,
        config.auto_publish_min_score,
    )
    logger.info(
        "post_scored post_id=%s score=%s decision=%s",
        post_id,
        analysis["lead_score"],
        decision,
    )

    conn.execute(
        "UPDATE threads_posts SET language = ?, problem_type = ?, "
        "lead_score = ?, relevance = ?, decision = ?, status = ? WHERE id = ?",
        (
            analysis["language"],
            analysis["problem_type"],
            analysis["lead_score"],
            str(analysis["relevant"]),
            decision,
            "analyzed" if decision == "skip" else "queued_for_review",
            row_id,
        ),
    )

    if (
        not analysis["relevant"]
        or decision == "skip"
        or not analysis["comment"]
    ):
        return "skipped"

    comment = shorten_cta_if_needed(
        analysis["comment"],
        analysis["problem_type"],
    )
    recent = _recent_published_comments(conn)
    if is_too_similar(comment, recent):
        logger.info("comment_too_similar_retrying post_id=%s", post_id)
        analysis = await deepseek.analyze_post(
            text,
            avoid_comments=recent,
        )
        comment = shorten_cta_if_needed(
            analysis["comment"],
            analysis["problem_type"],
        )

    conn.execute(
        "INSERT INTO generated_replies "
        "(post_id, generated_text, model, prompt_version, approved, "
        "published, created_at) VALUES (?, ?, ?, ?, 0, 0, ?)",
        (
            row_id,
            comment,
            config.deepseek_model,
            "v1",
            now_iso(),
        ),
    )
    reply_row_id = conn.execute(
        "SELECT last_insert_rowid() id"
    ).fetchone()["id"]
    touch_daily_metric(
        conn,
        today,
        relevant_posts=1,
        manual_reviews=1,
        website_cta_count=1,
        whatsapp_cta_count=1,
    )
    logger.info(
        "reply_queued_for_review post_id=%s decision=%s",
        post_id,
        decision,
    )

    try:
        from app.telegram_bot import notify_new_lead

        post_dict = dict(
            conn.execute(
                "SELECT * FROM threads_posts WHERE id = ?",
                (row_id,),
            ).fetchone()
        )
        await notify_new_lead(post_dict, reply_row_id, comment)
    except Exception:
        logger.exception("telegram_notify_failed post_id=%s", post_id)

    return "queued"


async def publish_approved_reply(
    threads: ThreadsClient,
    conn: sqlite3.Connection,
    reply_id: int,
) -> dict:
    """Publish only a human-approved reply and respect safety limits."""
    reply = conn.execute(
        "SELECT * FROM generated_replies WHERE id = ?",
        (reply_id,),
    ).fetchone()
    if reply is None:
        raise ValueError(f"no generated_replies row with id={reply_id}")
    if not reply["approved"]:
        raise PermissionError("reply is not approved")
    if reply["published"]:
        return {
            "status": "already_published",
            "threads_reply_id": reply["threads_reply_id"],
        }

    today = _today()
    published_today = conn.execute(
        "SELECT COUNT(*) c FROM generated_replies WHERE published = 1 "
        "AND date(published_at) = date('now')"
    ).fetchone()["c"]
    if published_today >= config.daily_reply_limit:
        logger.warning("daily_reply_limit_reached")
        return {"status": "daily_limit_reached"}

    post = conn.execute(
        "SELECT * FROM threads_posts WHERE id = ?",
        (reply["post_id"],),
    ).fetchone()

    if config.dry_run:
        logger.info(
            "dry_run_would_publish reply_id=%s post_id=%s",
            reply_id,
            post["threads_post_id"],
        )
        return {"status": "dry_run_ok"}

    try:
        threads_reply_id = await threads.publish_reply(
            reply["generated_text"],
            post["threads_post_id"],
        )
    except ThreadsAPIError as exc:
        logger.error(
            "publish_failed reply_id=%s status=%s",
            reply_id,
            exc.status_code,
        )
        conn.execute(
            "UPDATE generated_replies SET error = ? WHERE id = ?",
            (f"threads_api_error:{exc.status_code}", reply_id),
        )
        touch_daily_metric(conn, today, errors=1)
        return {
            "status": "error",
            "error": str(exc.status_code),
            "payload": exc.payload,
        }

    conn.execute(
        "UPDATE generated_replies SET published = 1, published_at = ?, "
        "threads_reply_id = ? WHERE id = ?",
        (now_iso(), threads_reply_id, reply_id),
    )
    conn.execute(
        "UPDATE authors SET last_contacted = ?, "
        "replies_count = replies_count + 1 WHERE author_id = ?",
        (now_iso(), post["author_id"]),
    )
    touch_daily_metric(conn, today, replies_published=1)
    logger.info(
        "reply_published reply_id=%s threads_reply_id=%s",
        reply_id,
        threads_reply_id,
    )
    return {
        "status": "published",
        "threads_reply_id": threads_reply_id,
    }
