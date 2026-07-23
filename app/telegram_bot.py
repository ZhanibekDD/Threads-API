"""Optional Telegram bot for human-in-the-loop approval of drafted replies.

If TELEGRAM_BOT_TOKEN is not set, the system still works: drafts simply sit
in generated_replies with approved=0 and can be approved via the CLI
(`python -m app.main list-queue` / `approve <id>`).
"""

import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from app.config import config
from app.db import session
from app.pipeline import publish_approved_reply
from app.threads_client import ThreadsClient

logger = logging.getLogger("telegram_bot")


def _keyboard(reply_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Опубликовать", callback_data=f"publish:{reply_id}"),
            InlineKeyboardButton("⏭ Пропустить", callback_data=f"skip:{reply_id}"),
        ],
        [
            InlineKeyboardButton("🚫 Заблокировать автора", callback_data=f"block:{reply_id}"),
        ],
    ])


def format_lead_message(post: dict, reply: dict) -> str:
    return (
        f"<b>Новый лид</b> (score {post['lead_score']}, {post['decision']})\n"
        f"Язык: {post['language']} | Проблема: {post['problem_type']}\n\n"
        f"<b>Пост:</b>\n{post['text'][:500]}\n\n"
        f"Ссылка: {post['permalink']}\n\n"
        f"<b>Черновик ответа:</b>\n{reply['generated_text']}"
    )


async def notify_new_lead(post: dict, reply_id: int, reply_text: str) -> None:
    """Send a standalone notification (own Bot instance, no polling needed).
    Safe to call from the search/ingest pipeline regardless of whether the
    long-running run-telegram process is up."""
    if not config.telegram_bot_token or not config.telegram_admin_chat_id:
        logger.info("telegram_not_configured — skipping notification, use CLI list-queue/approve")
        return
    text = format_lead_message(post, {"generated_text": reply_text})
    bot = Bot(token=config.telegram_bot_token)
    async with bot:
        await bot.send_message(
            chat_id=config.telegram_admin_chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=_keyboard(reply_id),
            disable_web_page_preview=True,
        )


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action, reply_id_str = query.data.split(":", 1)
    reply_id = int(reply_id_str)

    with session() as conn:
        reply = conn.execute("SELECT * FROM generated_replies WHERE id = ?", (reply_id,)).fetchone()
        if reply is None:
            await query.edit_message_text("Черновик не найден (уже обработан?).")
            return
        post = conn.execute("SELECT * FROM threads_posts WHERE id = ?", (reply["post_id"],)).fetchone()

        if action == "skip":
            conn.execute("UPDATE threads_posts SET status = 'skipped_by_human' WHERE id = ?", (post["id"],))
            await query.edit_message_text(query.message.text_html + "\n\n<i>Пропущено вручную.</i>",
                                            parse_mode="HTML")
            return

        if action == "block":
            conn.execute("UPDATE authors SET blocked = 1 WHERE author_id = ?", (post["author_id"],))
            conn.execute("UPDATE threads_posts SET status = 'skipped_by_human' WHERE id = ?", (post["id"],))
            await query.edit_message_text(query.message.text_html + "\n\n<i>Автор заблокирован.</i>",
                                            parse_mode="HTML")
            return

        if action == "publish":
            conn.execute("UPDATE generated_replies SET approved = 1 WHERE id = ?", (reply_id,))
            conn.commit()
            threads = ThreadsClient()
            try:
                result = await publish_approved_reply(threads, conn, reply_id)
            finally:
                await threads.aclose()
            await query.edit_message_text(
                query.message.text_html + f"\n\n<i>Результат: {result['status']}</i>",
                parse_mode="HTML",
            )
            return


def build_telegram_app() -> Application:
    if not config.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    app = Application.builder().token(config.telegram_bot_token).build()
    app.add_handler(CallbackQueryHandler(_on_callback))
    return app


def run_polling() -> None:
    app = build_telegram_app()
    logger.info("telegram_bot_started")
    app.run_polling()
