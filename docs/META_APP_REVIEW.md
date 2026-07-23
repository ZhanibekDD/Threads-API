# Meta App Review Package — ZakonExpert Threads Integration

Собрано по фактическому состоянию кода в этом репозитории. Каждая строка
таблиц ссылается на конкретный файл/метод/маршрут, который можно вызвать и
увидеть результат. Ничего не отправлено в Meta и не задеплоено в
продакшен — этот пакет только доводит проект до review-ready состояния,
финальную отправку делает пользователь отдельно.

**Приложение:** Threds (App ID `1601438514664176`)
**Тестовый аккаунт:** `zakonexpert.kzz` (Threads user id `27910545581903383`)
**Модель работы:** внутренний инструмент для одного собственного аккаунта
ZakonExpert, не многопользовательский публичный сервис. Ни один ответ на
чужой пост не публикуется без явного нажатия «Одобрить» в review-панели (или
эквивалентной CLI/Telegram-команды) — см. раздел 4.

---

## 1. Поэтапный запрос разрешений

### Этап 1 (подаётся первым): минимальный набор

| Permission | Зачем | Без этого разрешения |
|---|---|---|
| `threads_basic` | `GET /me` — подтвердить, к какому аккаунту относится токен, прежде чем что-либо делать | Нельзя даже проверить, что интеграция подключена к правильному аккаунту |
| `threads_keyword_search` | `GET /keyword_search` — найти публичные посты по юридическим ключевым словам ru/kk | Без этого нет самой функции поиска лидов — это ядро продукта |
| `threads_content_publish` | `POST /{user_id}/threads` + `/threads_publish` — публикация **собственного** образовательного контента ZakonExpert | Нельзя вести собственный профиль компании |

Этого набора достаточно, чтобы: искать посты, показывать их сотруднику в
review-панели с черновиком ответа, и вести собственный контент-маркетинг.
**Публикация ответа на чужой пост в этот набор не входит** — см. ниже.

### Этап 2 (подаётся отдельно, после одобрения этапа 1): `threads_manage_replies`

**Доказательство необходимости, а не предположение.** Мы напрямую проверили
это на живом приложении в App Dashboard (раздел «Разрешения и функции»,
описания самой Meta, не наши):

- `threads_content_publish`: *«The threads_content_publish permission allows
  an app to create and publish content on behalf of a Threads profile.»*
  — обобщённое разрешение на публикацию контента **от имени профиля**.
- `threads_manage_replies`: *«Разрешение threads_manage_replies позволяет
  приложению создать ответ от имени профиля Threads, скрыть или показать
  ответы на ветку, а также контролировать, кто может отвечать на ветку в
  профиле Threads.»* — отдельное, специально выделенное разрешение именно
  для **создания ответа** (reply), а не публикации нового поста.

Технически оба действия используют одну пару эндпоинтов (`POST
/{user_id}/threads` с параметром `reply_to_id`, затем `POST
/threads_publish`), но Meta различает их по разрешениям на уровне действия, а
не эндпоинта — это видно из того, что `threads_manage_replies` описан отдельно
и специально упоминает именно reply-функциональность, а не публикацию
контента в целом. В коде это `threads_client.py`:
[`create_reply_container()` / `publish_reply()`](../app/threads_client.py)
против [`create_own_post()`](../app/threads_client.py) — разные методы, разные
параметры (`reply_to_id` есть/нет), вызываются из разных мест
([`pipeline.publish_approved_reply()`](../app/pipeline.py) против
[`main.cmd_publish_own_content()`](../app/main.py)).

**Вывод:** без `threads_manage_replies` система физически не может
опубликовать ответ на чужой пост — только найти его и показать черновик.
Поэтому это разрешение обязательно для полного сценария продукта, но
**запрашивается вторым этапом**, после того как Meta увидит, что этап 1
работает аккуратно (человек одобряет каждый черновик, нет спама).

### Не запрашивается вообще

`threads_delete`, `threads_manage_mentions`, `threads_read_replies` — в коде
реализованы как CLI-утилиты (`delete-post`, `list-mentions`, `list-replies`,
см. [`main.py`](../app/main.py)) для внутренней диагностики и модерации
собственного контента, но не являются частью основного сценария ревью и не
запрашиваются в этом заходе, чтобы не расширять объём заявки. Можно добавить
отдельным этапом 3 позже, если понадобится.

`threads_location_tagging`, `threads_profile_discovery`,
`threads_share_to_instagram`, `threads_manage_insights` — нет вызывающего
кода вообще, не запрашиваются.

---

## 2. OAuth-поток (реализовано)

Минимальный, для одного собственного аккаунта — не многопользовательская
система.

| Шаг | Маршрут | Код |
|---|---|---|
| Начать подключение | `GET /threads/connect` | [`web.py:threads_connect()`](../app/web.py) — генерирует случайный `state` (`secrets.token_urlsafe(32)`), сохраняет в таблице `oauth_states`, редиректит на `https://threads.net/oauth/authorize` с нужным scope (этап 1 или 2 — `OAUTH_STAGE` в `.env`) |
| Приём кода | `GET /threads/callback` | [`web.py:threads_callback()`](../app/web.py) — проверяет `state` через [`oauth.consume_state()`](../app/oauth.py) (одноразовый, живёт 10 минут, удаляется при любой проверке — защита от replay и CSRF) |
| Обмен кода на токен | — | [`threads_client.exchange_code_for_token()`](../app/threads_client.py) — POST с form-телом (`data=`), не query-параметрами, чтобы `code` не попадал в логируемый URL |
| Перевод в long-lived | — | [`threads_client.exchange_for_long_lived_token()`](../app/threads_client.py) — `th_exchange_token`, токен живёт ~60 дней |
| Хранение | — | [`oauth.save_token()`](../app/oauth.py) — одна строка в таблице `oauth_tokens` (`id=1`, singleton — сознательно не многопользовательская схема) |
| Обновление | `python -m app.main refresh-token` | [`oauth.refresh_if_needed()`](../app/oauth.py) — обновляет, если до истечения меньше 5 дней (можно ставить в cron/Task Scheduler) |

**Требование «отсутствие токенов в логах, URL и интерфейсе» — как выполнено:**
- `code` передаётся в теле POST-запроса при обмене, не в query-строке
  вызова к Meta ([`threads_client.py`](../app/threads_client.py)).
- Логи: [`logging_utils.SecretRedactingFilter`](../app/logging_utils.py)
  вырезает `access_token=`, `code=`, `client_secret=` и Telegram bot-токены
  из любой строки лога до вывода.
- После callback пользователь сразу редиректится на чистый `/` без токена
  или кода в URL.
- В интерфейсе (`dashboard.html`) показывается только username и дата
  истечения токена — сам токен нигде не рендерится.
- Проверено тестами: [`tests/test_web_oauth.py`](../tests/test_web_oauth.py)
  (`test_full_oauth_roundtrip_saves_token_and_never_leaks_it` — проверяет
  фейковый токен на отсутствие в Location-заголовке, логах и HTML-странице).

---

## 3. Review-панель (браузер) — основной сценарий для ревьюера

Запуск: `python -m app.main run-web` → `http://127.0.0.1:5000/` (порт из
`REVIEW_PANEL_PORT`).

| Требование из ТЗ | Где в панели |
|---|---|
| Статус подключения аккаунта | Баннер вверху `/` — «Подключено: @username» / «Не подключено» + дата истечения токена ([`dashboard.html`](../app/templates/dashboard.html)) |
| Поле поиска | Форма `POST /search` — вводится ключевое слово, вызывает `keyword_search` на лету ([`web.py:search()`](../app/web.py)) |
| Список найденных постов | Секция «Очередь на подтверждение» — текст поста, ссылка (permalink) |
| Оценка релевантности | `lead_score`, `language`, `problem_type`, `decision` рядом с каждым постом |
| Черновик ответа | Показан прямо под постом (`generated_text` из DeepSeek) |
| Кнопки «Одобрить» / «Отклонить» | `POST /reply/<id>/approve` и `POST /reply/<id>/reject` |
| Подтверждение опубликованного ответа с permalink | После «Одобрить» — flash-сообщение со ссылкой на пост; список «Последние опубликованные» внизу страницы |

CLI (`run-once`, `list-queue`, `approve <id>` и т.д.) сохранён для диагностики
и сценариев без браузера (см. [README.md](../README.md)), но **основной путь
для ревьюера — браузер**, Token Generator из App Dashboard в этом пути больше
не используется (только для первоначальной выдачи тестового токена
разработчику, не как часть демонстрируемого сценария).

**Ограничение доступа к панели:** если задан `REVIEW_PANEL_PASSWORD`,
включается HTTP Basic Auth (см. [`web.py:_require_auth()`](../app/web.py)).
Без пароля панель предполагает работу только на `127.0.0.1` — не разворачивать
публично без пароля.

---

## 4. Где именно требуется подтверждение сотрудника (доказательство отсутствия автопубликации)

Не изменилось по сути с прошлой версии документа, только источник действия
теперь и панель, и CLI/Telegram:

- [`pipeline.py`](../app/pipeline.py), docstring: *"Nothing in this module
  ever calls threads_client.publish_reply() on its own."*
- [`pipeline.publish_approved_reply()`](../app/pipeline.py) выбрасывает
  `PermissionError`, если `approved` не установлен вручную.
- Установить `approved=1` можно только явным действием человека:
  кнопка «✅ Одобрить» в панели ([`web.py:approve_reply()`](../app/web.py)),
  кнопка в Telegram ([`telegram_bot.py`](../app/telegram_bot.py)), или
  `python -m app.main approve <id>`.
- Защита от похожих ответов: [`dedup.is_too_similar()`](../app/dedup.py).
- Лимиты: `DAILY_REPLY_LIMIT`, `AUTHOR_COOLDOWN_DAYS`, `POST_MAX_AGE_HOURS`.
- Тесты: [`tests/test_pipeline_publish.py`](../tests/test_pipeline_publish.py)
  (`test_publish_requires_human_approval` и др.), плюс новый
  [`tests/test_web_oauth.py`](../tests/test_web_oauth.py) для панели.

---

## 5. Готовые описания разрешений (EN / RU) для формы App Review

### threads_basic

**EN:** *"ZakonExpert's backend calls `GET /me` once when an employee connects
the account via our own review panel's OAuth flow (`/threads/connect` →
`/threads/callback`), to confirm which Threads account the newly obtained
token belongs to. This is a one-time identity check, not used to read or
display the user's post history."*

**RU:** *«Бэкенд ZakonExpert вызывает `GET /me` один раз при подключении
аккаунта сотрудником через собственный OAuth-поток панели ревью
(`/threads/connect` → `/threads/callback`), чтобы подтвердить, какому
Threads-аккаунту принадлежит только что полученный токен. Это разовая
проверка идентичности, а не чтение или показ истории постов пользователя.»*

### threads_keyword_search

**EN:** *"An authorized ZakonExpert employee enters a keyword in our review
panel (or the app runs a fixed list of Russian/Kazakh keywords describing
financial/legal distress in Kazakhstan, e.g. 'Kaspi арестовали счёт'). Results
are shown to the employee for manual review — the app never acts on search
results without a human decision."*

**RU:** *«Авторизованный сотрудник ZakonExpert вводит ключевое слово в нашей
review-панели (либо приложение прогоняет фиксированный список русских/
казахских ключевых слов о финансово-юридических проблемах в Казахстане,
например "Kaspi арестовали счёт"). Результаты показываются сотруднику для
ручного просмотра — приложение никогда не действует по результатам поиска
без решения человека.»*

### threads_content_publish

**EN:** *"A staff member manually triggers publishing of ZakonExpert's own
pre-written educational content to the business's own Threads profile via our
review panel or CLI. This is standard outbound content marketing for the
business's own account, not a reply to another user."*

**RU:** *«Сотрудник вручную запускает публикацию собственного заранее
написанного образовательного контента ZakonExpert в собственный Threads-
профиль компании через нашу review-панель или CLI. Это обычный исходящий
контент-маркетинг для собственного аккаунта, а не ответ другому пользователю.»*

### threads_manage_replies (Этап 2)

**EN:** *"After an employee reviews a specific public post (found via keyword
search) together with a drafted reply in our review panel, and explicitly
clicks 'Одобрить' ('Approve'), the app posts exactly one reply to that one
post. There is no bulk or automatic reply publishing — see
`pipeline.publish_approved_reply()`, which raises an error if the human
approval flag is not set, and is covered by automated tests."*

**RU:** *«После того как сотрудник в нашей review-панели просмотрел
конкретный публичный пост (найденный через поиск по ключевым словам) вместе
с черновиком ответа, и явно нажал "Одобрить", приложение публикует ровно один
ответ на этот один пост. Массовой или автоматической публикации ответов нет —
см. `pipeline.publish_approved_reply()`, которая выбрасывает ошибку, если
флаг подтверждения человеком не установлен, и покрыта автотестами.»*

---

## 6. Пошаговые инструкции для проверяющего Meta

1. Установить зависимости: `pip install -r requirements.txt`.
2. Заполнить `.env`: `THREADS_APP_ID`, `THREADS_APP_SECRET`,
   `THREADS_REDIRECT_URI` (должен совпадать с Valid OAuth Redirect URI в App
   Dashboard, например `http://127.0.0.1:5000/threads/callback` для
   локального ревью), `DEEPSEEK_API_KEY` (для генерации черновиков).
3. Запустить панель: `python -m app.main run-web`.
4. Открыть `http://127.0.0.1:5000/` — статус «Не подключено».
5. Нажать «Подключить аккаунт Threads» → пройти стандартный Threads Login
   диалог (в отличие от предыдущей версии этого документа — **не** через
   Token Generator в App Dashboard).
6. После редиректа обратно — статус «Подключено: @username».
7. Ввести ключевое слово в поле поиска (например «Kaspi»), нажать «Искать».
8. Просмотреть найденный пост, оценку релевантности и черновик ответа.
9. Нажать «✅ Одобрить» — при `DRY_RUN=true` (значение по умолчанию) ответ
   помечается одобренным, но реально не публикуется (это тоже видно в
   интерфейсе). Для реальной публикации на этапе 2-ревью установить
   `DRY_RUN=false` — тогда после «Одобрить» появится ссылка на реально
   опубликованный ответ.
10. Для CLI-диагностики (опционально): `python -m app.main whoami`,
    `python -m app.main list-queue`.

---

## 7. Финальный сценарий скринкаста (2–4 минуты)

| Время | Действие | Что показать |
|---|---|---|
| 0:00–0:20 | Запуск | Терминал: `python -m app.main run-web`, открыть `http://127.0.0.1:5000/` в браузере — статус «Не подключено» |
| 0:20–0:50 | OAuth-вход | Клик «Подключить аккаунт Threads» → стандартный экран авторизации Threads (домен threads.net, не наш) → согласие → редирект обратно, статус «Подключено: @zakonexpert.kzz» |
| 0:50–1:30 | Поиск | Ввести «Kaspi арестовали счёт» в поле поиска → «Искать» → появляется найденный публичный пост с текстом и ссылкой |
| 1:30–1:50 | Оценка релевантности | Указать на score/язык/тип проблемы рядом с постом |
| 1:50–2:20 | Черновик ответа | Показать сгенерированный черновик под постом (структура: полезный совет → о компании → сайт → WhatsApp, без давления и без гарантий) |
| 2:20–2:50 | Подтверждение сотрудником | Клик «✅ Одобрить» |
| 2:50–3:20 | Публикация | Flash-сообщение с permalink опубликованного ответа → открыть ссылку в новой вкладке, показать реальный ответ в Threads |
| 3:20–3:50 | (Если показываем threads_delete/manage_mentions отдельно — не входят в эту заявку) Показать раздел «Последние опубликованные» в панели как журнал действий | — |
| 3:50–4:00 | Итог | Одно предложение о том, что публикация возможна только после ручного одобрения, автоматической массовой рассылки нет |

Отдельно, если Meta требует продемонстрировать `threads_content_publish` явно
(может пересекаться с шагами выше): `python -m app.main publish-own-content
<id>` в терминале + обновлённая страница профиля с новым постом.

---

## 8. Реальные тестовые запросы и результаты (без токенов и секретов)

Из фактических вызовов, выполненных при подготовке этого пакета (токен ни
разу не выводился в консоль):

**`GET /me`**
```
{'id': '27910545581903383', 'username': 'zakonexpert.kzz'}
```

**`GET /debug_token`** (без самого токена в ответе)
```
{'is_valid': True, 'scopes': [... включая threads_keyword_search, threads_content_publish, threads_manage_replies ...], 'type': 'USER', 'user_id': '27910545581903383'}
```

**`GET /keyword_search?q=Kaspi&search_type=TOP`**
```
{'data': []}
```
Ожидаемо: `threads_keyword_search` пока на Standard Access (тестовая
песочница), не Advanced — именно это разрешение и есть предмет данной
заявки.

**Публикация + верификация + удаление (`threads_content_publish` +
`threads_delete`), реально выполнено:**
```
POST /{user_id}/threads (text="Тестовая публикация ZakonExpert — проверка интеграции Threads API.")
  -> creation_id = 18070218695443952
POST /{user_id}/threads_publish -> published id = 17909165169436566
GET /17909165169436566?fields=id,text,permalink,timestamp
  -> permalink = https://www.threads.com/@zakonexpert.kzz/post/Da0nY6ADpir
DELETE /17909165169436566 -> {'success': True, 'deleted_id': '17909165169436566'}
```

---

## 9. Необходимые URL и статус

| Требование | Статус | Комментарий |
|---|---|---|
| **Privacy Policy URL** | ✅ Есть | `https://zakonexpertt.kz/privacy` — проверено, доступна. |
| **Terms of Service** | ⚠️ Черновик готов, не опубликован | [`docs/website/terms.html`](website/terms.html) — я не могу опубликовать это сам (нет доступа к CMS/репозиторию сайта zakonexpertt.kz, это отдельная от Threds инфраструктура). Передать веб-разработчику сайта. |
| **User Data Deletion URL** | ⚠️ Черновик готов, не опубликован | [`docs/website/data-deletion.html`](website/data-deletion.html) — описывает честно, что хранится (токен, найденные публичные посты, черновики, журнал действий) и как это удалить/отозвать доступ. Тоже не опубликовано мной. |
| **Footer-ссылки** | ⚠️ Сниппет готов | [`docs/website/footer-snippet.html`](website/footer-snippet.html) — три ссылки для вставки в существующий футер. |
| **OAuth Redirect URI** | ✅ Реализовано в коде, не задеплоено | `/threads/connect` + `/threads/callback` работают локально (проверено тестами и вручную). Для реального ревью нужен публично доступный HTTPS URL — сейчас `THREADS_REDIRECT_URI=http://127.0.0.1:5000/threads/callback` (только локально). Нужно решить, где хостить панель (см. раздел 10). |

---

## 10. Итоговый список того, чего не хватает перед подачей на ревью

1. **Business Verification** в Meta Business Manager (условие для Advanced
   Access `threads_keyword_search`, не меняется этим пакетом).
2. **Опубликовать на сайте** `docs/website/terms.html`,
   `docs/website/data-deletion.html` и обновить футер по
   `docs/website/footer-snippet.html` — нужен доступ к CMS/репозиторию
   zakonexpertt.kz, которого у меня нет.
3. **Задеплоить review-панель** на публично доступный HTTPS-адрес (сейчас
   работает только на `127.0.0.1`) и обновить `THREADS_REDIRECT_URI` в `.env`
   и в App Dashboard → Threads API → Настройки → «URL обратного вызова для
   перенаправления».
4. **Записать реальный скринкаст** по сценарию из раздела 7.
5. **Заполнить форму App Review** — тексты из раздела 5 готовы почти
   дословно; сначала подать только `threads_basic` +
   `threads_keyword_search` + `threads_content_publish`, `threads_manage_replies`
   — вторым заходом (раздел 1).
6. Решить, ставить ли `REVIEW_PANEL_PASSWORD` перед тем, как панель станет
   доступна не только на localhost — сейчас без пароля.
