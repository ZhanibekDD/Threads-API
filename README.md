# ZakonExpert Threads Lead Monitor

Ищет в Threads (только через официальный Meta Threads API) публичные посты
людей из Казахстана с проблемами вида «арестовали Kaspi», «ЧСИ списывает
деньги», «запрет на выезд» и т.д., прогоняет их через DeepSeek для оценки
релевантности и генерации черновика ответа, и складывает всё в очередь на
**ручное подтверждение** через браузерную review-панель (или CLI/Telegram).

## Важная граница дизайна

**Нет ни одного пути кода, который публикует ответ в Threads без явного
подтверждения человеком.** Публикация происходит только через:

- кнопку «✅ Одобрить» в review-панели (`python -m app.main run-web`),
- кнопку «✅ Опубликовать» в Telegram, или
- `python -m app.main approve <id>` вручную.

Это сознательное решение, а не недоделанная фича: полностью автоматическая,
неотличимая от органической, рассылка комментариев людям в момент финансового
кризиса — это то, что я отказался строить. Дальше — обычный процесс: система
находит и предлагает черновик, человек решает, публиковать ли его.

Подробное обоснование разрешений Meta App Review, поэтапный план
(`threads_basic`/`threads_keyword_search`/`threads_content_publish` первым
заходом, `threads_manage_replies` вторым) и сценарий скринкаста — в
[docs/META_APP_REVIEW.md](docs/META_APP_REVIEW.md).

## Архитектура

```
app/
  config.py            переменные окружения, ключевые слова ru/kk
  db.py                схема SQLite (threads_posts, generated_replies,
                        authors, daily_metrics, deepseek_usage, own_content,
                        oauth_tokens, oauth_states)
  oauth.py              state/CSRF, обмен code -> long-lived token, хранение,
                        обновление — один аккаунт (singleton-строка)
  web.py                review-панель (Flask): /threads/connect,
                        /threads/callback, /, /search, /reply/<id>/approve|reject
  templates/dashboard.html  разметка review-панели
  lang_detect.py         эвристика ru/kk
  lead_scoring.py         расчёт lead_score из сигналов + пороги decide()
  dedup.py                похожесть текста, cooldown автора, возраст поста
  whatsapp.py              wa.me-ссылка с преднабранным текстом, UTM-ссылка на сайт
  threads_client.py       клиент graph.threads.net: OAuth, keyword_search,
                           mentions, replies, publish, delete (retry+backoff)
  deepseek_client.py       системный промпт, вызов DeepSeek, строгий парсинг JSON
  pipeline.py              поиск -> фильтры -> DeepSeek -> очередь; publish
                           только после approved=1
  telegram_bot.py          уведомления + кнопки подтверждения (опционально)
  content_generator.py     контент-план для собственного аккаунта ZakonExpert
  metrics.py                дневной отчёт + оценка стоимости DeepSeek
  main.py                   CLI (сохранён для диагностики)

docs/
  META_APP_REVIEW.md        полный пакет для Meta App Review
  website/                  черновики страниц /terms, /data-deletion,
                             footer-сниппет для zakonexpertt.kz (я их не
                             публиковал — нет доступа к CMS/репозиторию сайта)

tests/                      65 тестов, см. ниже
```

## Установка

```bash
python -m venv .venv
.venv\Scripts\activate       # Windows
pip install -r requirements.txt
```

`.env` уже заполнен доступными значениями (DeepSeek, App ID/Secret, текущий
токен как fallback). **Отсутствуют и должны быть получены через интерфейс**:
подключение аккаунта Threads (см. ниже — теперь это делается через
браузерную панель, а не вручную), `TELEGRAM_ADMIN_CHAT_ID` (опционально).

## Подключение аккаунта Threads (OAuth через панель)

Токен больше не нужно добывать вручную через Graph API Explorer — есть
встроенный OAuth-поток:

1. В App Dashboard → Threads API → Настройки → добавьте в «Действительные
   URI перенаправления для OAuth» адрес, совпадающий с `THREADS_REDIRECT_URI`
   в `.env` (по умолчанию `http://127.0.0.1:5000/threads/callback` для
   локального использования).
2. Запустите панель: `python -m app.main run-web`.
3. Откройте `http://127.0.0.1:5000/`, нажмите «Подключить аккаунт Threads».
4. Пройдите стандартный экран авторизации Threads.
5. После редиректа — статус «Подключено: @username», токен сохранён в БД
   (`oauth_tokens`), обновляется автоматически при приближении к истечению
   через `python -m app.main refresh-token` (можно поставить в cron).

`THREADS_ACCESS_TOKEN` в `.env` остаётся как **fallback** для CLI до первого
подключения через панель — после подключения приоритет у токена из БД
(см. `oauth.get_active_access_token()`).

### Telegram chat id (опционально)
1. Напишите вашему боту `/start` в Telegram.
2. Откройте `https://api.telegram.org/bot<ТОКЕН>/getUpdates` в браузере —
   в ответе будет `"chat":{"id": ЧИСЛО, ...}`.
3. Впишите это число в `TELEGRAM_ADMIN_CHAT_ID`.

## Команды запуска

```bash
# Review-панель в браузере — основной сценарий работы и для Meta App Review
python -m app.main run-web

# Один цикл поиска по фиксированным ключевым словам (уважает DRY_RUN)
python -m app.main run-once

# Бесконечный цикл с интервалом SEARCH_INTERVAL_MINUTES
python -m app.main run-loop

# Посмотреть очередь черновиков, ожидающих подтверждения
python -m app.main list-queue

# Подтвердить и опубликовать конкретный черновик (после DRY_RUN=false)
python -m app.main approve 42

# Обновить долгоживущий токен, если он скоро истекает
python -m app.main refresh-token

# Запустить Telegram-бота с кнопками подтверждения (нужен TELEGRAM_BOT_TOKEN)
python -m app.main run-telegram

# Диагностика Threads API
python -m app.main whoami
python -m app.main list-mentions
python -m app.main list-replies <media_id>
python -m app.main delete-post <media_id>

# Контент-план для собственного аккаунта (2 поста, 2 сценария, 5 stories,
# карусель раз в два дня)
python -m app.main generate-content
python -m app.main export-content       # CSV в ./exports
python -m app.main publish-own-content <id>   # реальная публикация одного черновика

# Дневной отчёт
python -m app.main report --date 2026-07-15
```

### Как включать автопубликацию своих постов
`AUTO_PUBLISH_OWN_CONTENT=false` по умолчанию — сгенерированный контент для
собственного аккаунта только сохраняется в БД/CSV. Публикация — отдельной
явной командой `publish-own-content <id>`, никогда не запускается сама.

### Запуск как постоянный сервис (Windows)
Через `nssm` или Планировщик заданий Windows, отдельными заданиями:
```
Программа: C:\Users\Zhanibek\Desktop\Threds\.venv\Scripts\python.exe
Аргументы: -m app.main run-web        (панель — оставить в браузере/фоне)
Аргументы: -m app.main run-loop       (фоновый поиск, если нужен без панели)
Аргументы: -m app.main run-telegram   (если используете Telegram-подтверждение)
Рабочая папка: C:\Users\Zhanibek\Desktop\Threds
```

## Тесты

```bash
python -m pytest -q
```

65 тестов, покрывают: определение ru/kk, расчёт lead_score и пороги
decide(), похожесть комментариев и cooldown автора, возраст поста,
WhatsApp-ссылку и UTM-ссылку на сайт, парсинг JSON от DeepSeek, DRY_RUN,
дневной лимит, обязательность approved=1 перед публикацией, идемпотентность
повторной публикации, обновление счётчика автора, **OAuth state (генерация,
одноразовость, истечение, CSRF-защита), полный roundtrip
connect→callback→сохранение токена через тестовый Flask-клиент, отсутствие
токена/code в логах и HTML-ответах, редактирование секретов в логах**.

Текущий результат: **65 passed**.

## Локальный автозапуск (вместо деплоя на сервер)

Пока работаем с одного компьютера — `scripts\start_review_panel.bat` и
`scripts\start_search_loop.bat` запускают панель и фоновый поиск. Чтобы они
поднимались сами при входе в Windows, добавьте ярлыки в `shell:startup` или
задачу в Планировщике заданий — подробно в
[docs/NEXT_STEPS.md](docs/NEXT_STEPS.md#3-локальный-автозапуск-сделано-вместо-деплоя-на-сервер).
Деплой на публичный сервер понадобится позже, не сейчас.

## Что дальше — подробно в docs/NEXT_STEPS.md

Коротко:
1. Business Verification уже пройдена. Осталось пройти оставшиеся пункты
   App Review для каждого нужного разрешения (готовые тексты — в
   `docs/META_APP_REVIEW.md`, раздел 5) и отправить заявку — точная навигация
   в [docs/NEXT_STEPS.md](docs/NEXT_STEPS.md#1-подать-заявку-на-app-review-в-meta).
2. Опубликовать `/terms`, `/data-deletion` и футер на zakonexpertt.kz —
   готовый промт для передачи веб-разработчику/другой AI-сессии лежит в
   [docs/website/AI_PROMPT.md](docs/website/AI_PROMPT.md) (у меня самого нет
   доступа к CMS/репозиторию сайта).
3. Ничего из этого не отправлено в Meta и не задеплоено — по вашей явной
   просьбе, ждёт отдельного указания.
