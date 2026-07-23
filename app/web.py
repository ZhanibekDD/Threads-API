"""Minimal review panel — the primary interface a reviewer/employee uses.

Single internal account, no multi-tenant/multi-user design. Every action
that touches the live Threads API for a *reply to someone else's post* still
goes through pipeline.publish_approved_reply(), which refuses to run without
an explicit approved=1 — the panel's "Одобрить" button is what sets that
flag, it does not bypass it.
"""

import asyncio
import logging
import secrets

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

from app import oauth
from app.config import config
from app.db import session as db_session
from app.deepseek_client import DeepSeekClient
from app.logging_utils import configure_logging
from app.pipeline import publish_approved_reply, search_and_ingest
from app.threads_client import ThreadsClient, ThreadsAPIError

logger = logging.getLogger("web")

app = Flask(__name__)
app.secret_key = config.review_panel_secret_key or secrets.token_hex(32)
if not config.review_panel_secret_key:
    logger.warning("REVIEW_PANEL_SECRET_KEY not set — using a random key that "
                    "changes every restart (sessions/flash messages won't persist across restarts).")
if not config.review_panel_password:
    logger.warning("REVIEW_PANEL_PASSWORD not set — the review panel has NO login. "
                    "Fine for localhost-only use; set a password before exposing this beyond localhost.")


# Meta calls these server-to-server (no browser, no login) — they carry
# their own cryptographic proof (signed_request, verified against our app
# secret), so they must stay reachable without HTTP Basic Auth.
_WEBHOOK_PATHS = {"/threads/deauthorize", "/threads/data-deletion"}


@app.before_request
def _require_auth():
    if request.path in _WEBHOOK_PATHS:
        return None
    if not config.review_panel_password:
        return None
    auth = request.authorization
    if not auth or auth.username != config.review_panel_username or auth.password != config.review_panel_password:
        return (
            "Authentication required", 401,
            {"WWW-Authenticate": 'Basic realm="ZakonExpert Review Panel"'},
        )
    return None


def _client_for(conn) -> ThreadsClient:
    token = oauth.get_active_access_token(conn)
    return ThreadsClient(access_token=token)


@app.route("/")
def dashboard():
    with db_session() as conn:
        status = oauth.connection_status(conn)
        queue = conn.execute(
            "SELECT gr.id as reply_id, tp.text, tp.permalink, tp.language, tp.problem_type, "
            "tp.lead_score, tp.decision, gr.generated_text "
            "FROM generated_replies gr JOIN threads_posts tp ON tp.id = gr.post_id "
            "WHERE gr.published = 0 AND tp.status = 'queued_for_review' "
            "ORDER BY tp.lead_score DESC"
        ).fetchall()
        recent = conn.execute(
            "SELECT gr.id, tp.permalink, gr.threads_reply_id, gr.published_at "
            "FROM generated_replies gr JOIN threads_posts tp ON tp.id = gr.post_id "
            "WHERE gr.published = 1 ORDER BY gr.id DESC LIMIT 10"
        ).fetchall()
    return render_template(
        "dashboard.html",
        status=status,
        queue=queue,
        recent=recent,
        dry_run=config.dry_run,
        oauth_stage=config.oauth_stage,
        threads_app_id_set=bool(config.threads_app_id),
        redirect_uri=config.threads_redirect_uri,
    )


@app.route("/threads/connect")
def threads_connect():
    if not config.threads_app_id or not config.threads_redirect_uri:
        flash("THREADS_APP_ID / THREADS_REDIRECT_URI не настроены в .env — сначала заполните их.", "error")
        return redirect(url_for("dashboard"))
    with db_session() as conn:
        state = oauth.generate_state(conn)
    return redirect(oauth.build_authorize_url(state))


@app.route("/threads/callback")
def threads_callback():
    error = request.args.get("error")
    if error:
        logger.warning("oauth_callback_denied error=%s", error)
        flash("Авторизация отклонена или прервана в Threads.", "error")
        return redirect(url_for("dashboard"))

    code = request.args.get("code")
    state = request.args.get("state")

    with db_session() as conn:
        if not oauth.consume_state(conn, state):
            logger.warning("oauth_callback_invalid_state")
            abort(400, description="Недействительный или истёкший state — попробуйте подключиться заново.")

        if not code:
            flash("Threads не вернул код авторизации.", "error")
            return redirect(url_for("dashboard"))

        async def _exchange_and_verify():
            # Both clients' full lifecycle (create -> use -> aclose) must run
            # under the SAME asyncio.run() call: httpx.AsyncClient binds its
            # transport to whichever event loop is active when it's used, and
            # asyncio.run() tears that loop down when it returns — closing a
            # client under a second, later asyncio.run() call hits a
            # "different loop" / "event loop is closed" error.
            client = ThreadsClient(access_token="")
            try:
                short_lived = await client.exchange_code_for_token(code)
                long_lived_inner = await client.exchange_for_long_lived_token(short_lived["access_token"])
            finally:
                await client.aclose()

            verify_client = ThreadsClient(access_token=long_lived_inner["access_token"])
            try:
                me_inner = await verify_client.get_me()
            finally:
                await verify_client.aclose()
            return long_lived_inner, me_inner

        try:
            long_lived, me = await_(_exchange_and_verify())
        except ThreadsAPIError:
            logger.exception("oauth_token_exchange_failed")
            flash("Не удалось обменять code на токен — см. логи сервера.", "error")
            return redirect(url_for("dashboard"))

        oauth.save_token(
            conn, long_lived["access_token"], long_lived.get("expires_in", 60 * 24 * 3600),
            me["id"], me.get("username", ""),
        )

    flash(f"Подключено: @{me.get('username', '')}", "success")
    return redirect(url_for("dashboard"))


@app.route("/threads/deauthorize", methods=["POST"])
def threads_deauthorize():
    """Meta calls this server-to-server when someone removes the app's
    access from their Threads/Facebook account settings. Single-account
    design: we just clear whatever token we have stored — there's nothing
    to look up per-user."""
    data = oauth.parse_signed_request(request.form.get("signed_request", ""), config.threads_app_secret)
    if data is None:
        logger.warning("deauthorize_callback_invalid_signature")
        abort(400)
    with db_session() as conn:
        conn.execute("DELETE FROM oauth_tokens WHERE id = 1")
        conn.commit()
    logger.info("threads_deauthorize_processed user_id=%s", data.get("user_id", "unknown"))
    return "", 200


@app.route("/threads/data-deletion", methods=["POST"])
def threads_data_deletion():
    """Meta's Data Deletion Request Callback. Must respond with a JSON
    object containing a status URL and confirmation code (documented Meta
    Platform protocol) — see threads_data_deletion_status() below."""
    data = oauth.parse_signed_request(request.form.get("signed_request", ""), config.threads_app_secret)
    if data is None:
        logger.warning("data_deletion_callback_invalid_signature")
        abort(400)

    confirmation_code = secrets.token_hex(8)
    with db_session() as conn:
        conn.execute("DELETE FROM oauth_tokens WHERE id = 1")
        conn.commit()
    logger.info("threads_data_deletion_requested user_id=%s code=%s",
                data.get("user_id", "unknown"), confirmation_code)

    status_url = url_for("threads_data_deletion_status", id=confirmation_code, _external=True)
    return jsonify({"url": status_url, "confirmation_code": confirmation_code})


@app.route("/threads/data-deletion-status")
def threads_data_deletion_status():
    code = request.args.get("id", "")
    return f"Запрос на удаление данных обработан. Код подтверждения: {code}"


@app.route("/search", methods=["POST"])
def search():
    query = (request.form.get("query") or "").strip()
    if not query:
        flash("Введите ключевое слово для поиска.", "error")
        return redirect(url_for("dashboard"))

    async def _do_search(conn):
        threads = _client_for(conn)
        deepseek = DeepSeekClient()
        try:
            return await search_and_ingest(threads, deepseek, conn, keywords=[query])
        finally:
            await threads.aclose()

    with db_session() as conn:
        try:
            summary = await_(_do_search(conn))
        except ThreadsAPIError as exc:
            flash(f"Ошибка Threads API: {exc.status_code} — {exc.payload}", "error")
            return redirect(url_for("dashboard"))

    flash(f"Поиск «{query}»: найдено {summary['found']}, в очередь добавлено {summary['queued']}.", "info")
    return redirect(url_for("dashboard"))


async def _do_approve(conn, reply_id: int) -> dict:
    threads = _client_for(conn)
    try:
        return await publish_approved_reply(threads, conn, reply_id)
    finally:
        await threads.aclose()


@app.route("/reply/<int:reply_id>/approve", methods=["POST"])
def approve_reply(reply_id: int):
    with db_session() as conn:
        conn.execute("UPDATE generated_replies SET approved = 1 WHERE id = ?", (reply_id,))
        conn.commit()
        result = await_(_do_approve(conn, reply_id))

        if result.get("status") == "published":
            post = conn.execute(
                "SELECT tp.permalink FROM generated_replies gr "
                "JOIN threads_posts tp ON tp.id = gr.post_id WHERE gr.id = ?", (reply_id,)
            ).fetchone()
            flash(f"Опубликовано. Пост: {post['permalink']}", "success")
        elif result.get("status") == "dry_run_ok":
            flash("DRY_RUN включён — ответ сохранён как одобренный, но НЕ отправлен в Threads.", "info")
        else:
            flash(f"Результат: {result.get('status')}", "error")

    return redirect(url_for("dashboard"))


@app.route("/reply/<int:reply_id>/reject", methods=["POST"])
def reject_reply(reply_id: int):
    with db_session() as conn:
        row = conn.execute("SELECT post_id FROM generated_replies WHERE id = ?", (reply_id,)).fetchone()
        if row is not None:
            conn.execute("UPDATE threads_posts SET status = 'skipped_by_human' WHERE id = ?", (row["post_id"],))
    flash("Черновик отклонён.", "info")
    return redirect(url_for("dashboard"))


def await_(coro):
    """Small helper: Flask's dev server is sync; the client library is async.
    Fine at this internal-tool scale (one operator, not high concurrency)."""
    return asyncio.run(coro)


def create_app() -> Flask:
    configure_logging()
    return app


if __name__ == "__main__":
    configure_logging()
    app.run(host=config.review_panel_host, port=config.review_panel_port, debug=False)
