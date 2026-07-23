from urllib.parse import quote, urlencode

from app.config import config

WHATSAPP_PREFILL_TEXT = (
    "Здравствуйте! Пишу из Threads. Нужна проверка ограничений. Мой ИИН:"
)

MAX_COMMENT_LEN = 420


def build_whatsapp_link(number: str | None = None, source: str = "threads") -> str:
    number = number or config.whatsapp_number
    text = f"{WHATSAPP_PREFILL_TEXT}"
    # wa.me does not support arbitrary query params other than `text`; the
    # `source` is folded into the prefilled text is intentionally avoided
    # (would confuse the recipient) — instead we track source separately
    # in our own DB (found_keyword / problem_type), and only add source as
    # a query param on our *own* domain links (see build_site_link).
    return f"https://wa.me/{number}?" + urlencode({"text": text})


def build_site_link(problem_type: str = "unknown", base_url: str | None = None) -> str:
    base_url = (base_url or config.site_url).rstrip("/") + "/"
    params = {
        "utm_source": "threads",
        "utm_medium": "comment",
        "utm_campaign": "lead_monitor",
        "utm_content": problem_type,
    }
    return base_url + "?" + urlencode(params)


def fallback_contact_line(number: str | None = None) -> str:
    number = number or config.whatsapp_number
    pretty = f"+{number[:1]} {number[1:4]} {number[4:7]}-{number[7:9]}-{number[9:11]}" if len(number) == 11 else f"+{number}"
    return f"zakonexpertt.kz и {pretty}"


def shorten_cta_if_needed(comment_with_link: str, problem_type: str = "unknown", number: str | None = None) -> str:
    """If the comment (with the full UTM link) exceeds MAX_COMMENT_LEN,
    fall back to the plain domain + phone number instead of the long URL."""
    if len(comment_with_link) <= MAX_COMMENT_LEN:
        return comment_with_link
    site_link = build_site_link(problem_type)
    return comment_with_link.replace(site_link, "zakonexpertt.kz")
