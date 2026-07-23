import re
from datetime import datetime, timedelta, timezone


def normalize_text(text: str) -> str:
    lowered = text.lower().strip()
    lowered = re.sub(r"\s+", " ", lowered)
    lowered = re.sub(r"[^\w\s]", "", lowered, flags=re.UNICODE)
    return lowered


def _tokenize(text: str) -> set[str]:
    return set(normalize_text(text).split())


def jaccard_similarity(a: str, b: str) -> float:
    tokens_a, tokens_b = _tokenize(a), _tokenize(b)
    if not tokens_a and not tokens_b:
        return 1.0
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    return intersection / union


def is_too_similar(candidate: str, recent_texts: list[str], threshold: float = 0.75) -> bool:
    return any(jaccard_similarity(candidate, existing) >= threshold for existing in recent_texts)


def author_in_cooldown(last_contacted_iso: str | None, cooldown_days: int, now: datetime | None = None) -> bool:
    if not last_contacted_iso:
        return False
    now = now or datetime.now(timezone.utc)
    last = datetime.fromisoformat(last_contacted_iso)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last < timedelta(days=cooldown_days)


def post_too_old(published_at_iso: str, max_age_hours: int, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    published = datetime.fromisoformat(published_at_iso)
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return now - published > timedelta(hours=max_age_hours)
