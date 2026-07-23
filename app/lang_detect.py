import re

# Letters that only exist in the Kazakh (Cyrillic) alphabet, not Russian.
KAZAKH_ONLY_CHARS = set("әғқңөұүһі" + "ӘҒҚҢӨҰҮҺІ")

KAZAKH_MARKER_WORDS = {
    "бұғатталды", "тыйым", "салынды", "жеке", "сот", "орындаушысы",
    "атқарушылық", "нотариустың", "жазбасы", "елден", "шығуға",
    "көлікке", "көлікті", "сата", "алмаймын", "мүлікке", "жалақыдан",
    "ұстап", "қалды", "қарызды", "төледім", "алынбады", "шотқа",
    "картам", "жсо", "ақша",
}

# Romanized Kazakh often shows up without diacritics, e.g. "buğattaldy".
KAZAKH_LATIN_MARKERS = {
    "buğattaldy", "buğattaldı", "tyiym", "salyndy", "ustap", "qaldy",
    "qarызdy",  # defensive, unlikely but harmless
}


def detect_language(text: str) -> str:
    """Best-effort ru/kk detection for short social posts.

    Returns "kk" for Kazakh, "ru" for Russian (default). This is a
    lightweight heuristic (character set + marker words), not a full
    language model — good enough to route posts to the right prompt.
    """
    if not text:
        return "ru"

    lowered = text.lower()

    if any(ch in KAZAKH_ONLY_CHARS for ch in text):
        return "kk"

    words = set(re.findall(r"[^\W\d_]+", lowered, flags=re.UNICODE))
    if words & KAZAKH_MARKER_WORDS:
        return "kk"
    if words & KAZAKH_LATIN_MARKERS:
        return "kk"

    return "ru"
