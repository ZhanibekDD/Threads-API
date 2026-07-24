import argparse
import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app import oauth
from app.config import config
from app.content_generator import (
    THREADS_POST_MAX_CHARS,
    export_content,
    generate_daily_content_plan,
    is_publish_time_now,
    parse_publish_times,
    publish_content_by_id,
    publish_next_approved,
)
from app.db import session
from app.deepseek_client import DeepSeekClient
from app.logging_utils import configure_logging
from app.metrics import daily_report, format_report
from app.pipeline import publish_approved_reply, search_and_ingest
from app.threads_client import ThreadsClient

logger = logging.getLogger("main")


def _threads_client(conn) -> ThreadsClient:
    return ThreadsClient(access_token=oauth.get_active_access_token(conn))


async def cmd_run_once() -> None:
    with session() as conn:
        threads = _threads_client(conn)
        deepseek = DeepSeekClient()
        try:
            summary = await search_and_ingest(threads, deepseek, conn)
        finally:
            await threads.aclose()
    logger.info("run_once_summary %s", summary)
    print(summary)


async def cmd_run_loop() -> None:
    interval = config.search_interval_minutes * 60
    logger.info(
        "run_loop_started interval_minutes=%s dry_run=%s",
        config.search_interval_minutes,
        config.dry_run,
    )
    while True:
        try:
            await cmd_run_once()
        except Exception:
            logger.exception("run_loop_cycle_failed")
        await asyncio.sleep(interval)


def cmd_list_queue() -> None:
    with session() as conn:
        rows = conn.execute(
            "SELECT gr.id as reply_id, tp.text, tp.lead_score, tp.decision, "
            "gr.generated_text FROM generated_replies gr "
            "JOIN threads_posts tp ON tp.id = gr.post_id "
            "WHERE gr.published = 0 ORDER BY tp.lead_score DESC"
        ).fetchall()
        if not rows:
            print("Очередь пуста.")
            return
        for row in rows:
            print(
                f"\n[{row['reply_id']}] score={row['lead_score']} "
                f"decision={row['decision']}"
            )
            print(f"  Пост: {row['text'][:200]}")
            print(f"  Черновик: {row['generated_text']}")


async def cmd_approve(reply_id: int) -> None:
    with session() as conn:
        conn.execute(
            "UPDATE generated_replies SET approved = 1 WHERE id = ?",
            (reply_id,),
        )
        conn.commit()
        threads = _threads_client(conn)
        try:
            result = await publish_approved_reply(threads, conn, reply_id)
        finally:
            await threads.aclose()
    print(result)


async def cmd_generate_content() -> None:
    with session() as conn:
        plan = await generate_daily_content_plan(DeepSeekClient(), conn)
    posts = sum(1 for item in plan if item["type"] == "post")
    print(
        f"Сгенерировано {len(plan)} материалов, "
        f"из них постов Threads: {posts}."
    )


def cmd_export_content() -> None:
    with session() as conn:
        path = export_content(conn)
    print(f"Экспортировано в {path}")


def cmd_report(date: str | None) -> None:
    date = date or datetime.now(timezone.utc).date().isoformat()
    with session() as conn:
        report = daily_report(conn, date)
    print(format_report(report))


def cmd_run_telegram() -> None:
    from app.telegram_bot import run_polling

    run_polling()


def cmd_run_web() -> None:
    from app.web import app as flask_app

    configure_logging()
    flask_app.run(
        host=config.review_panel_host,
        port=config.review_panel_port,
        debug=False,
    )


async def cmd_whoami() -> None:
    with session() as conn:
        threads = _threads_client(conn)
        try:
            me = await threads.get_me()
        finally:
            await threads.aclose()
    print(me)


async def cmd_refresh_token() -> None:
    with session() as conn:
        refreshed = await oauth.refresh_if_needed(conn, days_before_expiry=60)
    print(
        "Токен обновлён."
        if refreshed
        else "Обновление не требуется или OAuth-токен не подключён."
    )


async def cmd_list_mentions() -> None:
    with session() as conn:
        threads = _threads_client(conn)
        try:
            mentions = await threads.get_mentions()
        finally:
            await threads.aclose()
    for item in mentions.get("data", []):
        print(
            f"[{item.get('id')}] @{item.get('username')}: "
            f"{item.get('text', '')[:200]}"
        )
        print(f"    {item.get('permalink', '')}")


async def cmd_list_replies(media_id: str) -> None:
    with session() as conn:
        threads = _threads_client(conn)
        try:
            replies = await threads.get_replies(media_id)
        finally:
            await threads.aclose()
    for item in replies.get("data", []):
        print(
            f"[{item.get('id')}] @{item.get('username')}: "
            f"{item.get('text', '')[:200]}"
        )


async def cmd_publish_own_content(content_id: int) -> None:
    with session() as conn:
        threads = _threads_client(conn)
        try:
            result = await publish_content_by_id(threads, conn, content_id)
        finally:
            await threads.aclose()
    print(result)


def cmd_list_content() -> None:
    with session() as conn:
        rows = conn.execute(
            "SELECT id, type, language, pillar, funnel_stage, cta_code, "
            "approved, rejected, published, content, error FROM own_content "
            "WHERE published = 0 ORDER BY id"
        ).fetchall()
        if not rows:
            print("Нет несопубликованного контента. Запустите generate-content.")
            return
        for row in rows:
            if row["rejected"]:
                flag = "rejected"
            elif row["approved"]:
                flag = "approved"
            else:
                flag = "needs approval"
            print(
                f"\n[{row['id']}] {row['type']} ({row['language']}) — "
                f"{flag} — {len(row['content'])} символов"
            )
            print(
                f"  pillar={row['pillar'] or '-'} "
                f"stage={row['funnel_stage'] or '-'} "
                f"CTA={row['cta_code'] or '-'}"
            )
            if row["error"]:
                print(f"  ОШИБКА: {row['error']}")
            print(f"  {row['content'][:300]}")


def cmd_approve_content(content_id: int) -> None:
    with session() as conn:
        row = conn.execute(
            "SELECT id, type, content, published FROM own_content WHERE id = ?",
            (content_id,),
        ).fetchone()
        if row is None:
            print(f"own_content id={content_id} не найден")
            return
        if row["published"]:
            print(f"own_content id={content_id} уже опубликован")
            return
        if row["type"] != "post":
            print(f"own_content id={content_id} не является постом Threads")
            return
        length = len(row["content"])
        if length > THREADS_POST_MAX_CHARS:
            print(
                f"own_content id={content_id} НЕ одобрен: {length} символов, "
                f"лимит Threads — {THREADS_POST_MAX_CHARS}."
            )
            return
        conn.execute(
            "UPDATE own_content SET approved = 1, rejected = 0, error = NULL "
            "WHERE id = ?",
            (content_id,),
        )
    print(
        f"own_content id={content_id} одобрен — выйдет в ближайший слот "
        f"({config.own_content_publish_times}, {config.own_content_timezone}), "
        "если AUTO_PUBLISH_OWN_CONTENT=true."
    )


async def cmd_run_content_scheduler() -> None:
    times = parse_publish_times(config.own_content_publish_times)
    logger.info(
        "content_scheduler_started times=%s tz=%s auto_publish=%s",
        times,
        config.own_content_timezone,
        config.auto_publish_own_content,
    )
    last_slot_key = None
    while True:
        now = datetime.now(timezone.utc)
        if is_publish_time_now(times, now, config.own_content_timezone):
            local_now = now.astimezone(ZoneInfo(config.own_content_timezone))
            slot_key = (
                f"{local_now.date().isoformat()}_"
                f"{local_now.strftime('%H:%M')}"
            )
            if slot_key != last_slot_key:
                last_slot_key = slot_key
                try:
                    with session() as conn:
                        threads = _threads_client(conn)
                        try:
                            result = await publish_next_approved(threads, conn)
                        finally:
                            await threads.aclose()
                    logger.info("content_scheduler_tick result=%s", result)
                except Exception:
                    logger.exception("content_scheduler_tick_failed")
        await asyncio.sleep(30)


async def cmd_delete_post(media_id: str) -> None:
    with session() as conn:
        threads = _threads_client(conn)
        try:
            result = await threads.delete_post(media_id)
        finally:
            await threads.aclose()
    print(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.main")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run-once", help="Один цикл поиска и анализа")
    sub.add_parser("run-loop", help="Постоянный поиск лидов")
    sub.add_parser("list-queue", help="Показать очередь ответов")

    approve = sub.add_parser("approve", help="Одобрить и опубликовать ответ")
    approve.add_argument("reply_id", type=int)

    sub.add_parser("generate-content", help="Создать контент-план на сегодня")
    sub.add_parser("export-content", help="Экспортировать контент в CSV")
    sub.add_parser("list-content", help="Показать неопубликованный контент")

    approve_content = sub.add_parser(
        "approve-content",
        help="Одобрить собственный пост для расписания",
    )
    approve_content.add_argument("content_id", type=int)

    sub.add_parser(
        "run-content-scheduler",
        help="Публиковать одобренные посты по расписанию",
    )

    report = sub.add_parser("report", help="Дневной отчёт")
    report.add_argument("--date", default=None, help="YYYY-MM-DD")

    sub.add_parser("run-telegram", help="Запустить Telegram-бота")
    sub.add_parser("run-web", help="Запустить браузерную панель")
    sub.add_parser("whoami", help="Проверить Threads OAuth-токен")
    sub.add_parser("refresh-token", help="Обновить OAuth-токен")
    sub.add_parser("list-mentions", help="Показать упоминания")

    replies = sub.add_parser("list-replies", help="Показать ответы на пост")
    replies.add_argument("media_id")

    publish_own = sub.add_parser(
        "publish-own-content",
        help="Опубликовать собственный пост по id",
    )
    publish_own.add_argument("content_id", type=int)

    delete = sub.add_parser("delete-post", help="Удалить пост Threads")
    delete.add_argument("media_id")

    return parser


def main() -> None:
    configure_logging()
    args = build_parser().parse_args()

    if args.command == "run-once":
        asyncio.run(cmd_run_once())
    elif args.command == "run-loop":
        asyncio.run(cmd_run_loop())
    elif args.command == "list-queue":
        cmd_list_queue()
    elif args.command == "approve":
        asyncio.run(cmd_approve(args.reply_id))
    elif args.command == "generate-content":
        asyncio.run(cmd_generate_content())
    elif args.command == "export-content":
        cmd_export_content()
    elif args.command == "list-content":
        cmd_list_content()
    elif args.command == "approve-content":
        cmd_approve_content(args.content_id)
    elif args.command == "run-content-scheduler":
        asyncio.run(cmd_run_content_scheduler())
    elif args.command == "report":
        cmd_report(args.date)
    elif args.command == "run-telegram":
        cmd_run_telegram()
    elif args.command == "run-web":
        cmd_run_web()
    elif args.command == "whoami":
        asyncio.run(cmd_whoami())
    elif args.command == "refresh-token":
        asyncio.run(cmd_refresh_token())
    elif args.command == "list-mentions":
        asyncio.run(cmd_list_mentions())
    elif args.command == "list-replies":
        asyncio.run(cmd_list_replies(args.media_id))
    elif args.command == "publish-own-content":
        asyncio.run(cmd_publish_own_content(args.content_id))
    elif args.command == "delete-post":
        asyncio.run(cmd_delete_post(args.media_id))


if __name__ == "__main__":
    main()
