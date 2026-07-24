from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.content_generator import (
    ContentBrief,
    build_daily_briefs,
    generate_daily_content_plan,
    publish_content_by_id,
)
from app.db import now_iso


def _response(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


def _deepseek_with_responses(*texts: str):
    create = AsyncMock(side_effect=[_response(text) for text in texts])
    return SimpleNamespace(
        client=SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create)
            )
        )
    )


def test_daily_briefs_match_two_threads_slots_and_repurpose_topics():
    briefs = build_daily_briefs(day_of_year=200)
    posts = [brief for brief in briefs if brief.content_type == "post"]

    assert len(posts) == 2
    assert posts[0].language == "ru"
    assert posts[1].language == "kk"
    assert posts[0].topic != posts[1].topic
    assert any(brief.content_type == "reel_script" for brief in briefs)
    assert any(brief.content_type == "carousel" for brief in briefs)


def test_odd_day_keeps_second_post_in_russian_and_skips_carousel():
    briefs = build_daily_briefs(day_of_year=201)
    posts = [brief for brief in briefs if brief.content_type == "post"]

    assert posts[1].language == "ru"
    assert not any(brief.content_type == "carousel" for brief in briefs)


@pytest.mark.asyncio
async def test_generation_saves_funnel_metadata(conn):
    texts = [
        "Счёт заблокирован? Сначала выясните основание ареста. Напишите АРЕСТ.",
        "Запрет на выезд лучше проверять заранее. Напишите ВЫЕЗД.",
        "Видео: почему нельзя платить вслепую и что проверить первым.",
        "Сторис 1 вопрос. Сторис 2 проверка. Сторис 3 АРЕСТ.",
        "Сторис о запрете на выезд и кодовом слове ВЫЕЗД.",
        "Карусель: пять шагов проверки запрета на выезд.",
    ]
    deepseek = _deepseek_with_responses(*texts)

    plan = await generate_daily_content_plan(deepseek, conn, day_of_year=200)

    assert len(plan) == 6
    rows = conn.execute("SELECT * FROM own_content ORDER BY id").fetchall()
    assert len(rows) == 6
    assert rows[0]["pillar"]
    assert rows[0]["funnel_stage"]
    assert rows[0]["topic"]
    assert rows[0]["cta_code"]
    assert rows[0]["approved"] == 0
    assert rows[0]["rejected"] == 0


@pytest.mark.asyncio
async def test_duplicate_generation_is_rewritten_once(conn):
    duplicate = "Арестовали Kaspi сначала проверьте основание и документ"
    conn.execute(
        "INSERT INTO own_content (type, language, content, created_at) "
        "VALUES ('post', 'ru', ?, ?)",
        (duplicate, now_iso()),
    )
    conn.commit()

    brief = ContentBrief(
        content_type="post",
        language="ru",
        pillar="pain",
        funnel_stage="reach",
        topic="арест Kaspi",
        cta_code="АРЕСТ",
        angle="первый шаг",
    )
    rewritten = "Не знаете, кто заблокировал счёт? Получите постановление и сверьте взыскателя. Код АРЕСТ."
    deepseek = _deepseek_with_responses(duplicate, rewritten)

    with patch("app.content_generator.build_daily_briefs", return_value=[brief]):
        plan = await generate_daily_content_plan(deepseek, conn, day_of_year=1)

    assert len(plan) == 1
    assert plan[0]["content"] == rewritten
    assert deepseek.client.chat.completions.create.await_count == 2


@pytest.mark.asyncio
async def test_publish_content_by_id_marks_the_real_post_published(conn):
    conn.execute(
        "INSERT INTO own_content "
        "(type, language, content, created_at, approved, rejected) "
        "VALUES ('post', 'ru', 'Готовый пост', ?, 1, 0)",
        (now_iso(),),
    )
    conn.commit()
    content_id = conn.execute("SELECT id FROM own_content").fetchone()["id"]

    threads = AsyncMock()
    threads.create_own_post.return_value = "threads-post-777"
    result = await publish_content_by_id(threads, conn, content_id)

    assert result["status"] == "published"
    row = conn.execute(
        "SELECT * FROM own_content WHERE id = ?", (content_id,)
    ).fetchone()
    assert row["published"] == 1
    assert row["threads_post_id"] == "threads-post-777"
    assert row["published_at"] is not None


@pytest.mark.asyncio
async def test_publish_content_by_id_refuses_rejected_material(conn):
    conn.execute(
        "INSERT INTO own_content "
        "(type, language, content, created_at, approved, rejected) "
        "VALUES ('post', 'ru', 'Отклонённый пост', ?, 0, 1)",
        (now_iso(),),
    )
    conn.commit()
    content_id = conn.execute("SELECT id FROM own_content").fetchone()["id"]

    threads = AsyncMock()
    result = await publish_content_by_id(threads, conn, content_id)

    assert result["status"] == "rejected"
    threads.create_own_post.assert_not_called()
