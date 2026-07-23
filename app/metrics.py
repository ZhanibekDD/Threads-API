import os
import sqlite3
from collections import Counter

# Rough placeholder rates (USD per 1K tokens) — verify against the current
# DeepSeek pricing page before trusting the cost estimate for budgeting.
DEEPSEEK_PRICE_PER_1K_PROMPT = float(os.getenv("DEEPSEEK_PRICE_PER_1K_PROMPT", "0.00027"))
DEEPSEEK_PRICE_PER_1K_COMPLETION = float(os.getenv("DEEPSEEK_PRICE_PER_1K_COMPLETION", "0.0011"))


def daily_report(conn: sqlite3.Connection, date: str) -> dict:
    dm = conn.execute("SELECT * FROM daily_metrics WHERE date = ?", (date,)).fetchone()
    usage = conn.execute("SELECT * FROM deepseek_usage WHERE date = ?", (date,)).fetchone()

    problem_types = conn.execute(
        "SELECT problem_type, COUNT(*) c FROM threads_posts WHERE date(found_at) = ? "
        "GROUP BY problem_type ORDER BY c DESC", (date,)
    ).fetchall()
    languages = conn.execute(
        "SELECT language, COUNT(*) c FROM threads_posts WHERE date(found_at) = ? "
        "GROUP BY language ORDER BY c DESC", (date,)
    ).fetchall()

    prompt_tokens = usage["prompt_tokens"] if usage else 0
    completion_tokens = usage["completion_tokens"] if usage else 0
    cost = (prompt_tokens / 1000 * DEEPSEEK_PRICE_PER_1K_PROMPT
            + completion_tokens / 1000 * DEEPSEEK_PRICE_PER_1K_COMPLETION)

    return {
        "date": date,
        "searches": dm["searches"] if dm else 0,
        "posts_found": dm["posts_found"] if dm else 0,
        "relevant_posts": dm["relevant_posts"] if dm else 0,
        "manual_reviews": dm["manual_reviews"] if dm else 0,
        "replies_published": dm["replies_published"] if dm else 0,
        "website_cta_count": dm["website_cta_count"] if dm else 0,
        "whatsapp_cta_count": dm["whatsapp_cta_count"] if dm else 0,
        "errors": dm["errors"] if dm else 0,
        "problem_types": {r["problem_type"]: r["c"] for r in problem_types},
        "languages": {r["language"]: r["c"] for r in languages},
        "deepseek_requests": usage["requests"] if usage else 0,
        "deepseek_prompt_tokens": prompt_tokens,
        "deepseek_completion_tokens": completion_tokens,
        "deepseek_estimated_cost_usd": round(cost, 4),
    }


def format_report(report: dict) -> str:
    lines = [f"Отчёт за {report['date']}", "-" * 30]
    lines.append(f"Поисков: {report['searches']}")
    lines.append(f"Найдено постов: {report['posts_found']}")
    lines.append(f"Релевантных: {report['relevant_posts']}")
    lines.append(f"На ручной проверке: {report['manual_reviews']}")
    lines.append(f"Опубликовано ответов: {report['replies_published']}")
    lines.append(f"CTA на сайт: {report['website_cta_count']} | CTA на WhatsApp: {report['whatsapp_cta_count']}")
    lines.append(f"Ошибок: {report['errors']}")
    lines.append(f"Языки: {report['languages']}")
    lines.append(f"Типы проблем: {report['problem_types']}")
    lines.append(
        f"DeepSeek: {report['deepseek_requests']} запросов, "
        f"{report['deepseek_prompt_tokens']} prompt / {report['deepseek_completion_tokens']} completion токенов, "
        f"~${report['deepseek_estimated_cost_usd']}"
    )
    return "\n".join(lines)
