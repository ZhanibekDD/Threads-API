"""Generate conversion-oriented content for ZakonExpert's own Threads account.

The generator creates a small daily funnel instead of random isolated posts:
reach -> trust -> conversion. Every generated post still requires human approval
before the scheduler can publish it.
"""

import csv
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import config
from app.db import now_iso
from app.dedup import is_too_similar
from app.deepseek_client import DeepSeekClient
from app.threads_client import ThreadsAPIError, ThreadsClient

logger = logging.getLogger("content_generator")

THREADS_POST_MAX_CHARS = 500


OWN_CONTENT_SYSTEM_PROMPT = """Ты — сильный контент-маркетолог юридического сервиса ZakonExpert в Казахстане.
Сайт: zakonexpertt.kz. WhatsApp: +7 775 299-87-38.

Цель — не писать формальную рекламу, а вызвать узнавание проблемы, дать человеку
полезный первый шаг и перевести подходящего клиента в WhatsApp.

Обязательные правила:
1. Первый ряд — короткий сильный крючок без кликбейта и капслока.
2. Один пост раскрывает одну проблему и даёт один конкретный следующий шаг.
3. Не выдумывай клиентов, выигранные дела, суммы, сроки и результаты. Если нет
   фактов конкретного дела, называй ситуацию типовой, а не реальным кейсом.
4. Не обещай 100% результат, снятие за один день или обязательную отмену долга.
5. Не оскорбляй ЧСИ, банки, нотариусов и взыскателей.
6. Не выдавай ZakonExpert за государственный орган.
7. Не проси публиковать ИИН или документы в комментариях.
8. Избегай канцелярита, одинаковых вступлений и фраз «обращайтесь к нам».
9. В продающем посте используй только один призыв: написать в WhatsApp указанное
   кодовое слово. Не перегружай одновременно телефоном, сайтом и несколькими CTA.
10. Пиши на указанном языке. Возвращай только готовый текст без пояснений и кавычек."""


@dataclass(frozen=True)
class ContentBrief:
    content_type: str
    language: str
    pillar: str
    funnel_stage: str
    topic: str
    cta_code: str
    angle: str


POST_BLUEPRINTS = [
    {
        "pillar": "pain",
        "funnel_stage": "reach",
        "topic": "арест банковского счёта или Kaspi",
        "cta_code": "АРЕСТ",
        "angle": "Объясни неожиданную правду: сначала нужно установить документ и основание, а не платить вслепую.",
    },
    {
        "pillar": "mistake",
        "funnel_stage": "trust",
        "topic": "исполнительная надпись нотариуса",
        "cta_code": "НАДПИСЬ",
        "angle": "Покажи одну частую ошибку после внезапного ареста и безопасный первый шаг.",
    },
    {
        "pillar": "instruction",
        "funnel_stage": "reach",
        "topic": "долг оплачен, но ограничения остались",
        "cta_code": "СНЯТЬ",
        "angle": "Дай короткий порядок проверки: оплата, окончание производства, отдельное снятие мер.",
    },
    {
        "pillar": "urgency",
        "funnel_stage": "conversion",
        "topic": "запрет на выезд из Казахстана",
        "cta_code": "ВЫЕЗД",
        "angle": "Объясни, почему нельзя ждать аэропорта и какие документы нужно проверить заранее.",
    },
    {
        "pillar": "myth",
        "funnel_stage": "trust",
        "topic": "удержание денег из зарплаты",
        "cta_code": "УДЕРЖАНИЕ",
        "angle": "Разбери миф, что любое удержание автоматически законно только потому, что его делает ЧСИ.",
    },
    {
        "pillar": "objection",
        "funnel_stage": "conversion",
        "topic": "рассрочка или график платежей",
        "cta_code": "ГРАФИК",
        "angle": "Ответь на возражение человека, который не может внести половину долга сразу.",
    },
    {
        "pillar": "warning",
        "funnel_stage": "reach",
        "topic": "сообщение 1414 об исполнительном производстве",
        "cta_code": "1414",
        "angle": "Покажи, почему сообщение нельзя игнорировать и что проверить в тот же день.",
    },
    {
        "pillar": "checklist",
        "funnel_stage": "conversion",
        "topic": "запрет или арест автомобиля",
        "cta_code": "АВТО",
        "angle": "Дай мини-чеклист перед продажей автомобиля или попыткой снять ограничение.",
    },
]


CONTENT_TYPES = {
    "post": (
        "Создай пост для Threads длиной 280–480 символов.\n"
        "Тема: {topic}\nУгол: {angle}\nЭтап воронки: {funnel_stage}\n"
        "Кодовое слово: {cta_code}\nЯзык: {language}\n"
        "Структура: крючок -> 1 полезный шаг -> короткое объяснение -> CTA.\n"
        "Для reach CTA должен быть мягким. Для trust покажи компетентность. "
        "Для conversion закончи фразой написать в WhatsApp слово {cta_code}."
    ),
    "reel_script": (
        "Создай сценарий Reels/TikTok на 30–45 секунд по теме {topic}. "
        "Угол: {angle}. Язык: {language}. Формат: крючок, 3 коротких кадра, CTA "
        "написать в WhatsApp слово {cta_code}. Не выдумывай кейсы."
    ),
    "story_idea": (
        "Создай серию из 3 Stories по теме {topic}. Язык: {language}. "
        "1 — вопрос или опрос, 2 — полезный ответ, 3 — CTA написать слово "
        "{cta_code} в WhatsApp. Дай текст каждого экрана."
    ),
    "carousel": (
        "Создай карусель из 5 слайдов по теме {topic}. Язык: {language}. "
        "Для каждого слайда дай заголовок и короткий текст. Последний слайд — "
        "CTA написать в WhatsApp слово {cta_code}. Не выдумывай результаты."
    ),
}


def build_daily_briefs(day_of_year: int | None = None) -> list[ContentBrief]:
    """Build a compact, deterministic daily funnel.

    Two Threads posts match the two default publication slots. Supporting
    formats repurpose the same topics instead of spending model calls on a
    random pile of unrelated ideas.
    """
    if day_of_year is None:
        day_of_year = datetime.now(tz=ZoneInfo(config.own_content_timezone)).timetuple().tm_yday

    first = POST_BLUEPRINTS[day_of_year % len(POST_BLUEPRINTS)]
    second = POST_BLUEPRINTS[(day_of_year + 3) % len(POST_BLUEPRINTS)]
    second_language = "kk" if day_of_year % 2 == 0 else "ru"

    briefs = [
        ContentBrief("post", "ru", **first),
        ContentBrief("post", second_language, **second),
        ContentBrief("reel_script", "ru", **first),
        ContentBrief("story_idea", "ru", **first),
        ContentBrief("story_idea", second_language, **second),
    ]
    if day_of_year % 2 == 0:
        briefs.append(ContentBrief("carousel", second_language, **second))
    return briefs


def _recent_content(conn: sqlite3.Connection, limit: int = 20) -> list[str]:
    rows = conn.execute(
        "SELECT content FROM own_content WHERE content <> '' ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [row["content"] for row in rows]


async def generate_daily_content_plan(
    deepseek: DeepSeekClient,
    conn: sqlite3.Connection,
    day_of_year: int | None = None,
) -> list[dict]:
    """Generate a daily funnel and save only non-empty, non-duplicate drafts."""
    recent = _recent_content(conn)
    generated: list[dict] = []

    for brief in build_daily_briefs(day_of_year):
        text = await _generate_one(deepseek, brief, avoid_texts=recent)
        if not text:
            continue

        if is_too_similar(text, recent, threshold=0.62):
            logger.info(
                "own_content_too_similar_retry type=%s topic=%s",
                brief.content_type,
                brief.topic,
            )
            text = await _generate_one(
                deepseek,
                brief,
                avoid_texts=recent,
                rewrite=True,
            )

        if not text or is_too_similar(text, recent, threshold=0.62):
            logger.warning(
                "own_content_skipped_duplicate type=%s topic=%s",
                brief.content_type,
                brief.topic,
            )
            continue

        conn.execute(
            "INSERT INTO own_content "
            "(type, language, content, created_at, pillar, funnel_stage, topic, cta_code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                brief.content_type,
                brief.language,
                text,
                now_iso(),
                brief.pillar,
                brief.funnel_stage,
                brief.topic,
                brief.cta_code,
            ),
        )
        item = {
            "type": brief.content_type,
            "language": brief.language,
            "pillar": brief.pillar,
            "funnel_stage": brief.funnel_stage,
            "topic": brief.topic,
            "cta_code": brief.cta_code,
            "content": text,
        }
        generated.append(item)
        recent.append(text)

    conn.commit()
    return generated


async def _generate_one(
    deepseek: DeepSeekClient,
    brief: ContentBrief,
    avoid_texts: list[str] | None = None,
    rewrite: bool = False,
) -> str:
    template = CONTENT_TYPES[brief.content_type]
    prompt = template.format(
        topic=brief.topic,
        angle=brief.angle,
        funnel_stage=brief.funnel_stage,
        cta_code=brief.cta_code,
        language=brief.language,
    )

    if avoid_texts:
        examples = "\n---\n".join(avoid_texts[:5])
        prompt += (
            "\n\nНе повторяй структуру, крючки и формулировки этих недавних материалов:\n"
            + examples
        )
    if rewrite:
        prompt += "\n\nПредыдущий вариант оказался слишком похожим. Полностью смени крючок и структуру."

    try:
        response = await deepseek.client.chat.completions.create(
            model=config.deepseek_model,
            messages=[
                {"role": "system", "content": OWN_CONTENT_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.75,
        )
        raw = response.choices[0].message.content
        return (raw or "").strip()
    except Exception:
        logger.exception("own_content_generation_failed type=%s", brief.content_type)
        return ""


def export_content(conn: sqlite3.Connection, out_dir: str = "./exports") -> str:
    """Export un-exported own_content rows to CSV and mark them exported."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    rows = conn.execute("SELECT * FROM own_content WHERE exported = 0 ORDER BY id").fetchall()
    out_path = Path(out_dir) / f"content_{now_iso().replace(':', '-')} .csv"
    out_path = Path(str(out_path).replace(" -", "-"))
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "id",
                "type",
                "language",
                "pillar",
                "funnel_stage",
                "topic",
                "cta_code",
                "content",
                "approved",
                "published",
                "created_at",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["id"],
                    row["type"],
                    row["language"],
                    row["pillar"],
                    row["funnel_stage"],
                    row["topic"],
                    row["cta_code"],
                    row["content"],
                    row["approved"],
                    row["published"],
                    row["created_at"],
                ]
            )
    ids = [row["id"] for row in rows]
    if ids:
        conn.executemany("UPDATE own_content SET exported = 1 WHERE id = ?", [(row_id,) for row_id in ids])
    return str(out_path)


def parse_publish_times(raw: str) -> list[str]:
    """'10:00, 18:00' -> ['10:00', '18:00']. Ignores blanks/whitespace."""
    return [value.strip() for value in raw.split(",") if value.strip()]


def current_local_slot(now: datetime, tz_name: str) -> str:
    """Return current HH:MM in the configured timezone."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))
    return now.astimezone(ZoneInfo(tz_name)).strftime("%H:%M")


def is_publish_time_now(publish_times: list[str], now: datetime, tz_name: str) -> bool:
    return current_local_slot(now, tz_name) in publish_times


async def publish_content_by_id(
    threads: ThreadsClient,
    conn: sqlite3.Connection,
    content_id: int,
) -> dict:
    """Publish one reviewed Threads post immediately."""
    row = conn.execute(
        "SELECT * FROM own_content WHERE id = ?",
        (content_id,),
    ).fetchone()
    if row is None:
        return {"status": "not_found"}
    if row["type"] != "post":
        return {"status": "not_post"}
    if row["rejected"]:
        return {"status": "rejected"}
    if row["published"]:
        return {
            "status": "already_published",
            "id": row["id"],
            "threads_post_id": row["threads_post_id"],
        }

    length = len(row["content"])
    if length > THREADS_POST_MAX_CHARS:
        error = (
            f"content is {length} chars, over the {THREADS_POST_MAX_CHARS} "
            "limit — needs manual editing"
        )
        conn.execute(
            "UPDATE own_content SET approved = 0, error = ? WHERE id = ?",
            (error, row["id"]),
        )
        conn.commit()
        logger.warning("own_content_too_long id=%s length=%s", row["id"], length)
        return {
            "status": "content_too_long",
            "id": row["id"],
            "length": length,
        }

    try:
        post_id = await threads.create_own_post(row["content"])
    except ThreadsAPIError as exc:
        error = f"threads_api_error:{exc.status_code}"
        conn.execute(
            "UPDATE own_content SET approved = 0, error = ? WHERE id = ?",
            (error, row["id"]),
        )
        conn.commit()
        logger.error(
            "own_content_publish_failed id=%s status=%s",
            row["id"],
            exc.status_code,
        )
        return {
            "status": "error",
            "id": row["id"],
            "error": error,
            "payload": exc.payload,
        }

    conn.execute(
        "UPDATE own_content SET approved = 1, published = 1, published_at = ?, "
        "threads_post_id = ?, exported = 1, error = NULL WHERE id = ?",
        (now_iso(), post_id, row["id"]),
    )
    conn.commit()
    logger.info("own_content_published id=%s threads_post_id=%s", row["id"], post_id)
    return {"status": "published", "id": row["id"], "threads_post_id": post_id}


async def publish_next_approved(
    threads: ThreadsClient,
    conn: sqlite3.Connection,
) -> dict:
    """Publish the oldest approved, non-rejected Threads post."""
    row = conn.execute(
        "SELECT * FROM own_content "
        "WHERE type = 'post' AND approved = 1 AND rejected = 0 "
        "AND published = 0 AND error IS NULL ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return {"status": "nothing_to_publish"}

    if not config.auto_publish_own_content:
        logger.info("auto_publish_own_content_disabled_would_publish id=%s", row["id"])
        return {"status": "disabled", "id": row["id"]}

    return await publish_content_by_id(threads, conn, row["id"])
