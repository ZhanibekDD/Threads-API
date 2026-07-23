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
    """Single source of truth for which token to use: the DB-stored OAuth
    token (set via /threads/connect in the review panel) takes priority over
    the THREADS_ACCESS_TOKEN env var — same resolution the web panel uses."""
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
    logger.info("run_loop_started interval_minutes=%s dry_run=%s", config.search_interval_minutes, config.dry_run)
    while True:
        await cmd_run_once()
        await asyncio.sleep(interval)


def cmd_list_queue() -> None:
    with session() as conn:
        rows = conn.execute(
            "SELECT gr.id as reply_id, tp.text, tp.lead_score, tp.decision, gr.generated_text "
            "FROM generated_replies gr JOIN threads_posts tp ON tp.id = gr.post_id "
            "WHERE gr.published = 0 ORDER BY tp.lead_score DESC"
        ).fetchall()
        if not rows:
            print("Очередь пуста.")
            return
        for r in rows:
            print(f"\n[{r['reply_id']}] score={r['lead_score']} decision={r['decision']}")
            print(f"  Пост: {r['text'][:200]}")
            print(f"  Черновик: {r['generated_text']}")


async def cmd_approve(reply_id: int) -> None:
    with session() as conn:
        conn.execute("UPDATE generated_replies SET approved = 1 WHERE id = ?", (reply_id,))
        conn.commit()
        threads = _threads_client(conn)
        try:
            result = await publish_approved_reply(threads, conn, reply_id)
        finally:
            await threads.aclose()
    print(result)


async def cmd_generate_content() -> None:
    with session() as conn:
        deepseek = DeepSeekClient()
        plan = await generate_daily_content_plan(deepseek, conn)
    print(f"Сгенерировано {len(plan)} единиц контента.")


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
    flask_app.run(host=config.review_panel_host, port=config.review_panel_port, debug=False)


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
    print("Токен обновлён." if refreshed else "Обновление не требуется (или токен ещё не подключён через /threads/connect).")


async def cmd_list_mentions() -> None:
    with session() as conn:
        threads = _threads_client(conn)
        try:
            mentions = await threads.get_mentions()
        finally:
            await threads.aclose()
    for item in mentions.get("data", []):
        print(f"[{item.get('id')}] @{item.get('username')}: {item.get('text', '')[:200]}")
        print(f"    {item.get('permalink', '')}")


async def cmd_list_replies(media_id: str) -> None:
    with session() as conn:
        threads = _threads_client(conn)
        try:
            replies = await threads.get_replies(media_id)
        finally:
            await threads.aclose()
    for item in replies.get("data", []):
        print(f"[{item.get('id')}] @{item.get('username')}: {item.get('text', '')[:200]}")


async def cmd_publish_own_content(content_id: int) -> None:
    """Publish a single own_content row (human trigger: you must already have
    reviewed the text via `export-content` / the DB before running this).
    Does not touch generated_replies — that path stays gated by approve."""
    with session() as conn:
        row = conn.execute("SELECT * FROM own_content WHERE id = ?", (content_id,)).fetchone()
        if row is None:
            print(f"own_content id={content_id} не найден")
            return
        threads = _threads_client(conn)
        try:
            post_id = await threads.create_own_post(row["content"])
        finally:
            await threads.aclose()
        conn.execute("UPDATE own_content SET exported = 1 WHERE id = ?", (content_id,))
    print(f"Опубликовано: threads_post_id={post_id}")


def cmd_list_content() -> None:
    with session() as conn:
        rows = conn.execute(
            "SELECT id, type, language, approved, published, content, error FROM own_content "
            "WHERE published = 0 ORDER BY id"
        ).fetchall()
        if not rows:
            print("Нет несопубликованного контента. Запустите generate-content.")
            return
        for r in rows:
            flag = "approved" if r["approved"] else "needs approval"
            print(f"\n[{r['id']}] {r['type']} ({r['language']}) — {flag} — {len(r['content'])} символов")
            if r["error"]:
                print(f"  ОШИБКА: {r['error']}")
            print(f"  {r['content'][:200]}")


def cmd_approve_content(content_id: int) -> None:
    with session() as conn:
        row = conn.execute("SELECT id, content FROM own_content WHERE id = ?", (content_id,)).fetchone()
        if row is None:
            print(f"own_content id={content_id} не найден")
            return
        length = len(row["content"])
        if length > THREADS_POST_MAX_CHARS:
            print(f"own_content id={content_id} НЕ одобрен: {length} символов, "
                  f"лимит Threads — {THREADS_POST_MAX_CHARS}. Сократите текст и повторите.")
            return
        conn.execute("UPDATE own_content SET approved = 1, error = NULL WHERE id = ?", (content_id,))
    print(f"own_content id={content_id} одобрен — выйдет в ближайший слот "
          f"({config.own_content_publish_times}, {config.own_content_timezone}), "
          f"если AUTO_PUBLISH_OWN_CONTENT=true.")


async def cmd_run_content_scheduler() -> None:
    times = parse_publish_times(config.own_content_publish_times)
    logger.info("content_scheduler_started times=%s tz=%s auto_publish=%s",
                times, config.own_content_timezone, config.auto_publish_own_content)
    last_slot_key = None
    while True:
        now = datetime.now(timezone.utc)
        if is_publish_time_now(times, now, config.own_content_timezone):
            # Dedup key = calendar date + HH:MM slot in the configured tz, so
            # a slot fires at most once even though we poll every 30s.
            local_now = now.astimezone(ZoneInfo(config.own_content_timezone))
            slot_key = f"{local_now.date().isoformat()}_{local_now.strftime('%H:%M')}"
            if slot_key != last_slot_key:
                # Set BEFORE attempting: a slot fires at most once no matter
                # what happens below — an unexpected error must never turn
                # into a crash-restart-retry loop within the same slot.
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

    sub.add_parser("run-once", help="Один цикл поиска+анализа (DRY_RUN уважается)")
    sub.add_parser("run-loop", help="Бесконечный цикл с интервалом SEARCH_INTERVAL_MINUTES")
    sub.add_parser("list-queue", help="Показать черновики, ожидающие подтверждения")

    p_approve = sub.add_parser("approve", help="Подтвердить и опубликовать черновик по id")
    p_approve.add_argument("reply_id", type=int)

    sub.add_parser("generate-content", help="Сгенерировать контент-план на сегодня")
    sub.add_parser("export-content", help="Экспортировать несохранённый контент в CSV")
    sub.add_parser("list-content", help="Показать неопубликованный собственный контент")

    p_approve_content = sub.add_parser("approve-content", help="Одобрить собственный контент для авто-публикации по расписанию")
    p_approve_content.add_argument("content_id", type=int)

    sub.add_parser("run-content-scheduler", help="Публиковать одобренный собственный контент по расписанию (OWN_CONTENT_PUBLISH_TIMES)")

    p_report = sub.add_parser("report", help="Дневной отчёт")
    p_report.add_argument("--date", default=None, help="YYYY-MM-DD, по умолчанию сегодня (UTC)")

    sub.add_parser("run-telegram", help="Запустить Telegram-бота подтверждения")
    sub.add_parser("run-web", help="Запустить review-панель (браузер) — основной сценарий для ревьюера")

    sub.add_parser("whoami", help="GET /me — проверить токен и аккаунт (threads_basic)")
    sub.add_parser("refresh-token", help="Обновить долгоживущий токен, если он скоро истекает")
    sub.add_parser("list-mentions", help="Показать упоминания аккаунта (threads_manage_mentions)")

    p_replies = sub.add_parser("list-replies", help="Показать ответы на пост (threads_read_replies)")
    p_replies.add_argument("media_id")

    p_publish_own = sub.add_parser("publish-own-content", help="Опубликовать сохранённый собственный контент по id (threads_content_publish)")
    p_publish_own.add_argument("content_id", type=int)

    p_delete = sub.add_parser("delete-post", help="Удалить пост по Threads media id (threads_delete)")
    p_delete.add_argument("media_id")

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
