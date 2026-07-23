from app.whatsapp import (
    MAX_COMMENT_LEN,
    build_site_link,
    build_whatsapp_link,
    fallback_contact_line,
    shorten_cta_if_needed,
)


def test_whatsapp_link_contains_number_and_prefill_text():
    link = build_whatsapp_link(number="77752998738")
    assert link.startswith("https://wa.me/77752998738?")
    assert "text=" in link
    assert "%D0%98%D0%98%D0%9D" in link or "ИИН" in link or "+" in link  # url-encoded prefill present


def test_site_link_has_utm_params():
    link = build_site_link(problem_type="account_arrest")
    assert "utm_source=threads" in link
    assert "utm_medium=comment" in link
    assert "utm_campaign=lead_monitor" in link
    assert "utm_content=account_arrest" in link
    assert link.startswith("https://zakonexpertt.kz/?")


def test_shorten_cta_keeps_short_comment_untouched():
    short = "Короткий комментарий без ссылки."
    assert shorten_cta_if_needed(short) == short


def test_shorten_cta_falls_back_to_plain_domain_when_too_long():
    link = build_site_link("account_arrest")
    comment = ("Очень длинный комментарий про арест счета и что делать дальше. " * 5) + link
    assert len(comment) > MAX_COMMENT_LEN
    result = shorten_cta_if_needed(comment, "account_arrest")
    assert link not in result
    assert "zakonexpertt.kz" in result


def test_fallback_contact_line_formats_number():
    line = fallback_contact_line("77752998738")
    assert "zakonexpertt.kz" in line
    assert "+7 775 299-87-38" in line
