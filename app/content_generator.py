"""Generates ZakonExpert's own Threads content (not replies to strangers).

This is normal outbound marketing for the business's own account, drafted
by DeepSeek and stored for review/export. Auto-posting is gated by
AUTO_PUBLISH_OWN_CONTENT (default false) and still goes through
ThreadsClient.create_own_post() only when a human runs the publish command.
"""

import csv
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import config
from app.db import now_iso
from app.deepseek_client import DeepSeekClient
from app.threads_client import ThreadsAPIError, ThreadsClient

logger = logging.getLogger("content_generator")

# Threads rejects text posts over this length ("Param text must be at most
# 500 characters long"). Checked before ever calling the API — see
# publish_next_approved().
THREADS_POST_MAX_CHARS = 500

OWN_CONTENT_SYSTEM_PROMPT = """Ты — контент-маркетолог юридического сервиса ZakonExpert (Казахстан).
Сайт: https://zakonexpertt.kz/  WhatsApp: +7 775 299-87-38.

Создавай полезный, не рекламный по тону контент о снятии арестов и ограничений
(Kaspi/Halyk, ЧСИ, исполнительная надпись нотариуса, запрет на выезд, арест
имущества, долг оплачен — ограничения остались, рассрочка/отсрочка).

Правила: без гарантий 100% результата, без оскорблений ЧСИ/банков/нотариусов,
без выдачи себя за государственный орган, упоминай сайт и WhatsApp как способ
связи, в конце можно предложить прислать ИИН и документы в WhatsApp (не в
публикации). Пиши на указанном языке (ru или kk). Отвечай только текстом
публикации, без пояснений и без кавычек вокруг текста."""

CONTENT_TYPES = {
    "post": "Напиши короткий полезный пост для Threads (до 500 символов) на тему: {topic}. Язык: {language}.",
    "reel_script": "Напиши сценарий для Reels/TikTok 30-45 секунд на тему: {topic}. Язык: {language}. Формат: реплики/кадры по пунктам.",
    "story_idea": "Дай короткую идею для Stories (1-2 предложения + предлагаемый текст на экране) на тему: {topic}. Язык: {language}.",
    "carousel": "Напиши структуру карусели из 5-6 слайдов на тему: {topic}. Язык: {language}. Для каждого слайда: заголовок + короткий текст.",
}

DEFAULT_TOPICS = [
    "что делать, если Kaspi заблокировал счёт",
    "как узнать, кто наложил арест на имущество",
    "разница между арестом счёта и запретом на выезд",
    "что делать, если долг оплачен, а ограничения не сняли",
    "как работает исполнительная надпись нотариуса",
    "как ZakonExpert проверяет исполнительное производство",
    "почему нельзя игнорировать сообщение 1414",
    "как договориться о рассрочке с ЧСИ",
]


async def generate_daily_content_plan(deepseek: DeepSeekClient, conn: sqlite3.Connection) -> list[dict]:
    """2 posts + 2 reel scripts + 5 story ideas + (every other day) 1 carousel,
    alternating ru/kk, saved to own_content."""
    import random

    plan_spec = [("post", 2), ("reel_script", 2), ("story_idea", 5)]
    day_of_year = __import__("datetime").datetime.utcnow().timetuple().tm_yday
    if day_of_year % 2 == 0:
        plan_spec.append(("carousel", 1))

    generated = []
    for content_type, count in plan_spec:
        for i in range(count):
            language = "ru" if i % 2 == 0 else "kk"
            topic = random.choice(DEFAULT_TOPICS)
            text = await _generate_one(deepseek, content_type, topic, language)
            conn.execute(
                "INSERT INTO own_content (type, language, content, created_at) VALUES (?, ?, ?, ?)",
                (content_type, language, text, now_iso()),
            )
            generated.append({"type": content_type, "language": language, "content": text})
    return generated


async def _generate_one(deepseek: DeepSeekClient, content_type: str, topic: str, language: str) -> str:
    template = CONTENT_TYPES[content_type]
    prompt = template.format(topic=topic, language=language)
    try:
        response = await deepseek.client.chat.completions.create(
            model=config.deepseek_model,
            messages=[
                {"role": "system", "content": OWN_CONTENT_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()
    except Exception:
        logger.exception("own_content_generation_failed type=%s", content_type)
        return ""


def export_content(conn: sqlite3.Connection, out_dir: str = "./exports") -> str:
    """Export un-exported own_content rows to a CSV file, mark them exported."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    rows = conn.execute("SELECT * FROM own_content WHERE exported = 0 ORDER BY id").fetchall()
    out_path = Path(out_dir) / f"content_{now_iso().replace(':', '-')}.csv"
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "type", "language", "content", "created_at"])
        for r in rows:
            writer.writerow([r["id"], r["type"], r["language"], r["content"], r["created_at"]])
    ids = [r["id"] for r in rows]
    if ids:
        conn.executemany("UPDATE own_content SET exported = 1 WHERE id = ?", [(i,) for i in ids])
    return str(out_path)


def parse_publish_times(raw: str) -> list[str]:
    """'10:00, 18:00' -> ['10:00', '18:00']. Ignores blanks/whitespace."""
    return [t.strip() for t in raw.split(",") if t.strip()]


def current_local_slot(now: datetime, tz_name: str) -> str:
    """Current HH:MM in the configured timezone, for comparing against
    OWN_CONTENT_PUBLISH_TIMES. `now` should be timezone-aware (any tz) or
    naive-UTC; converted to `tz_name` either way."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))
    return now.astimezone(ZoneInfo(tz_name)).strftime("%H:%M")


def is_publish_time_now(publish_times: list[str], now: datetime, tz_name: str) -> bool:
    return current_local_slot(now, tz_name) in publish_times


async def publish_next_approved(threads: ThreadsClient, conn: sqlite3.Connection) -> dict:
    """Publishes the oldest human-approved, not-yet-published `post`-type
    row. Only `type='post'` rows are ever auto-published this way — reel
    scripts / story ideas / carousels are source material for other formats,
    not directly postable to Threads as-is.

    A human must have already set approved=1 (CLI `approve-content <id>` or
    the panel) — this function never publishes anything nobody has reviewed,
    it only controls *when* an already-approved post goes out.
    """
    row = conn.execute(
        "SELECT * FROM own_content WHERE type = 'post' AND approved = 1 AND published = 0 "
        "AND error IS NULL ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return {"status": "nothing_to_publish"}

    if not config.auto_publish_own_content:
        logger.info("auto_publish_own_content_disabled_would_publish id=%s", row["id"])
        return {"status": "disabled", "id": row["id"]}

    # Checked up front so an oversized post can never reach the API (and
    # never permanently block every post behind it — see the `error IS NULL`
    # filter above, which skips it on future ticks once flagged).
    length = len(row["content"])
    if length > THREADS_POST_MAX_CHARS:
        error = f"content is {length} chars, over the {THREADS_POST_MAX_CHARS} limit — needs manual editing"
        conn.execute("UPDATE own_content SET approved = 0, error = ? WHERE id = ?", (error, row["id"]))
        conn.commit()
        logger.warning("own_content_too_long id=%s length=%s", row["id"], length)
        return {"status": "content_too_long", "id": row["id"], "length": length}

    try:
        post_id = await threads.create_own_post(row["content"])
    except ThreadsAPIError as exc:
        error = f"threads_api_error:{exc.status_code}"
        conn.execute("UPDATE own_content SET approved = 0, error = ? WHERE id = ?", (error, row["id"]))
        conn.commit()
        logger.error("own_content_publish_failed id=%s status=%s", row["id"], exc.status_code)
        return {"status": "error", "id": row["id"], "error": error}

    conn.execute(
        "UPDATE own_content SET published = 1, published_at = ?, threads_post_id = ?, exported = 1 "
        "WHERE id = ?",
        (now_iso(), post_id, row["id"]),
    )
    conn.commit()
    logger.info("own_content_published id=%s threads_post_id=%s", row["id"], post_id)
    return {"status": "published", "id": row["id"], "threads_post_id": post_id}
