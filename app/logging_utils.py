import logging
import re

_SECRET_PATTERNS = [
    re.compile(r"(access_token=)[^&\s]+", re.IGNORECASE),
    re.compile(r"([?&]code=)[^&\s]+", re.IGNORECASE),
    re.compile(r"(client_secret=)[^&\s]+", re.IGNORECASE),
    re.compile(r"(api[_-]?key[\"']?\s*[:=]\s*[\"']?)[\w-]{10,}", re.IGNORECASE),
    re.compile(r"(bot)\d{6,}:[\w-]{20,}", re.IGNORECASE),
]


class SecretRedactingFilter(logging.Filter):
    """Belt-and-suspenders scrub in case a token/key ends up in a log line.
    The real defense is: never pass secrets into log calls to begin with."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        redacted = msg
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(SecretRedactingFilter())
    logging.basicConfig(level=level, handlers=[handler],
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")
