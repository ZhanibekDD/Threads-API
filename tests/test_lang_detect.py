from app.lang_detect import detect_language


def test_detects_russian_by_default():
    assert detect_language("Арестовали счет в Каспи, что делать?") == "ru"


def test_detects_kazakh_by_special_chars():
    assert detect_language("Kaspi шотыма тыйым салынды, не істеу керек?") == "kk"


def test_detects_kazakh_marker_words_without_diacritics():
    assert detect_language("kaspi shot buğattaldy komekshi kerek") == "kk"


def test_empty_text_defaults_to_russian():
    assert detect_language("") == "ru"
