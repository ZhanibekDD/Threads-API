import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return int(val)


@dataclass(frozen=True)
class Config:
    deepseek_api_key: str = field(default_factory=lambda: os.getenv("DEEPSEEK_API_KEY", ""))
    deepseek_base_url: str = field(default_factory=lambda: os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    deepseek_model: str = field(default_factory=lambda: os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"))

    threads_app_id: str = field(default_factory=lambda: os.getenv("THREADS_APP_ID", ""))
    threads_app_secret: str = field(default_factory=lambda: os.getenv("THREADS_APP_SECRET", ""))
    threads_access_token: str = field(default_factory=lambda: os.getenv("THREADS_ACCESS_TOKEN", ""))
    threads_user_id: str = field(default_factory=lambda: os.getenv("THREADS_USER_ID", ""))
    threads_api_version: str = field(default_factory=lambda: os.getenv("THREADS_API_VERSION", "v1.0"))
    threads_redirect_uri: str = field(default_factory=lambda: os.getenv("THREADS_REDIRECT_URI", ""))

    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_admin_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_ADMIN_CHAT_ID", ""))

    site_url: str = field(default_factory=lambda: os.getenv("SITE_URL", "https://zakonexpertt.kz/"))
    whatsapp_number: str = field(default_factory=lambda: os.getenv("WHATSAPP_NUMBER", "77752998738"))

    database_path: str = field(default_factory=lambda: os.getenv("DATABASE_PATH", "./data/threds.db"))

    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", True))
    manual_review_min_score: int = field(default_factory=lambda: _int("MANUAL_REVIEW_MIN_SCORE", 70))
    auto_publish_min_score: int = field(default_factory=lambda: _int("AUTO_PUBLISH_MIN_SCORE", 90))
    daily_reply_limit: int = field(default_factory=lambda: _int("DAILY_REPLY_LIMIT", 20))
    author_cooldown_days: int = field(default_factory=lambda: _int("AUTHOR_COOLDOWN_DAYS", 30))
    post_max_age_hours: int = field(default_factory=lambda: _int("POST_MAX_AGE_HOURS", 12))
    search_interval_minutes: int = field(default_factory=lambda: _int("SEARCH_INTERVAL_MINUTES", 60))
    auto_publish_own_content: bool = field(default_factory=lambda: _bool("AUTO_PUBLISH_OWN_CONTENT", False))
    # HH:MM (24h), comma-separated, interpreted in `own_content_timezone`.
    # The scheduler only ever publishes rows a human has already approved —
    # this controls WHEN, not WHETHER. See content_generator.publish_next_approved().
    own_content_publish_times: str = field(default_factory=lambda: os.getenv("OWN_CONTENT_PUBLISH_TIMES", "10:00,18:00"))
    own_content_timezone: str = field(default_factory=lambda: os.getenv("OWN_CONTENT_TIMEZONE", "Asia/Almaty"))

    # Which App Review stage's scopes to request during /threads/connect.
    # 1 = threads_basic, threads_keyword_search, threads_content_publish.
    # 2 = stage 1 + threads_manage_replies (needed to actually publish a
    # reply to someone else's post — see docs/META_APP_REVIEW.md).
    oauth_stage: int = field(default_factory=lambda: _int("OAUTH_STAGE", 1))

    review_panel_secret_key: str = field(default_factory=lambda: os.getenv("REVIEW_PANEL_SECRET_KEY", ""))
    review_panel_username: str = field(default_factory=lambda: os.getenv("REVIEW_PANEL_USERNAME", "admin"))
    review_panel_password: str = field(default_factory=lambda: os.getenv("REVIEW_PANEL_PASSWORD", ""))
    review_panel_port: int = field(default_factory=lambda: _int("REVIEW_PANEL_PORT", 5000))
    # 127.0.0.1 for bare-metal local use. Must be 0.0.0.0 inside Docker —
    # binding to loopback inside a container makes it unreachable even
    # through published ports, since Docker's bridge network doesn't proxy
    # into the container's loopback interface.
    review_panel_host: str = field(default_factory=lambda: os.getenv("REVIEW_PANEL_HOST", "127.0.0.1"))

    def ensure_data_dir(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)


config = Config()

KEYWORDS_RU = [
    "арестовали kaspi", "заблокировали kaspi", "kaspi арест", "арест счета",
    "арестовали карту", "карта под арестом", "чси списал деньги",
    "чси арестовал счет", "исполнительное производство", "исполнительная надпись",
    "нотариус взыскивает долг", "запрет на выезд", "не выпускают из казахстана",
    "запрет на автомобиль", "не могу продать машину", "арест квартиры",
    "удерживают зарплату", "оплатил долг арест не сняли",
    "долг оплачен ограничения остались", "пришло сообщение 1414",
]

KEYWORDS_KK = [
    "kaspi бұғатталды", "kaspi шотым бұғатталды", "шотқа тыйым салынды",
    "картам бұғатталды", "жеке сот орындаушысы", "жсо ақша ұстап қалды",
    "атқарушылық іс жүргізу", "нотариустың атқарушылық жазбасы",
    "елден шығуға тыйым", "көлікке тыйым салынды", "көлікті сата алмаймын",
    "мүлікке тыйым салынды", "жалақыдан ақша ұстап қалды",
    "қарызды төледім тыйым алынбады",
    # без спецсимволов / разговорные варианты
    "kaspi shot buğattaldy", "kaspi karta buğattaldy", "shotka tyiym salyndy",
    "zhso aqsha ustap qaldy", "kolikke tyiym salyngan", "mulikke tyiym salyngan",
]

ALL_KEYWORDS = KEYWORDS_RU + KEYWORDS_KK
