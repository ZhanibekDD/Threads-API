from datetime import datetime, timedelta, timezone

from app.dedup import author_in_cooldown, is_too_similar, post_too_old


def test_identical_comments_are_too_similar():
    a = "Сначала нужно проверить, кто наложил арест. zakonexpertt.kz WhatsApp +7 775 299-87-38"
    assert is_too_similar(a, [a]) is True


def test_different_comments_are_not_too_similar():
    a = "Сначала нужно проверить, кто наложил арест на счет."
    b = "Проверьте запрет на выезд через сайт госуслуг Казахстана прямо сейчас."
    assert is_too_similar(a, [b], threshold=0.75) is False


def test_no_recent_comments_never_too_similar():
    assert is_too_similar("что угодно", []) is False


def test_author_cooldown_active_when_recently_contacted():
    now = datetime.now(timezone.utc)
    last_contacted = (now - timedelta(days=5)).isoformat()
    assert author_in_cooldown(last_contacted, cooldown_days=30, now=now) is True


def test_author_cooldown_expired_after_period():
    now = datetime.now(timezone.utc)
    last_contacted = (now - timedelta(days=31)).isoformat()
    assert author_in_cooldown(last_contacted, cooldown_days=30, now=now) is False


def test_author_never_contacted_not_in_cooldown():
    assert author_in_cooldown(None, cooldown_days=30) is False


def test_post_too_old():
    now = datetime.now(timezone.utc)
    published = (now - timedelta(hours=13)).isoformat()
    assert post_too_old(published, max_age_hours=12, now=now) is True


def test_post_within_age_limit():
    now = datetime.now(timezone.utc)
    published = (now - timedelta(hours=1)).isoformat()
    assert post_too_old(published, max_age_hours=12, now=now) is False
