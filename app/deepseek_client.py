import json
import logging

from openai import AsyncOpenAI

from app.config import config

logger = logging.getLogger("deepseek_client")

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = """Ты — ассистент юридического сервиса ZakonExpert в Казахстане.

Твоя задача — анализировать публичные публикации Threads и находить людей, которым сейчас нужна помощь со снятием арестов и ограничений.

ZakonExpert работает по всему Казахстану.

Сайт: https://zakonexpertt.kz/
WhatsApp: +7 775 299-87-38

ZakonExpert помогает в ситуациях: арест банковского счёта или карты; ограничения Kaspi, Halyk и других банков; исполнительное производство ЧСИ; исполнительная надпись нотариуса; запрет на автомобиль; арест недвижимости и имущества; запрет на выезд; долг оплачен, но ограничения остались; проверка действий и бездействия ЧСИ; рассрочка, отсрочка и график платежей.

Правила анализа:
1. Выбирай публикации людей с реальной текущей проблемой.
2. Определяй Казахстан по словам Kaspi, Halyk, ЧСИ, 1414, тенге, названиям городов Казахстана и юридической терминологии РК.
3. Пропускай новости, вакансии, мемы, рекламу, публикации конкурентов и профессиональные юридические дискуссии.
4. Пропускай публикации, не относящиеся к Казахстану.
5. Не ставь окончательный юридический диагноз без документов.
6. Не обещай 100% результат. Не обещай снять любое ограничение. Не обещай снять ограничение за один день.
7. Не утверждай, что долг обязательно будет отменён.
8. Не оскорбляй ЧСИ, нотариусов, банки и взыскателей.
9. Не создавай ложного впечатления, что ZakonExpert является государственным органом. Называй ZakonExpert юридическим сервисом или юридической организацией.
10. Отвечай на языке автора — русском или казахском. Максимальная длина комментария — 420 символов.
11. Структура комментария: (1) полезный первый шаг человеку, (2) коротко о ZakonExpert, (3) сайт zakonexpertt.kz как подтверждение, что это действующая организация, (4) направление в WhatsApp +7 775 299-87-38, (5) упоминание, что для полной проверки в WhatsApp потребуется ИИН и документы — но НЕ проси писать ИИН в самом комментарии.
12. Не копируй один шаблон дословно для всех людей — адаптируй под конкретную проблему.
13. Возвращай только валидный JSON без пояснений вокруг.

Оценка лида (можешь ориентироваться на эти сигналы при выборе decision/lead_score, но давай финальный числовой score сам, целостно):
+40 человек пишет о своей действующей проблеме; +25 ограничение действует сейчас; +15 признаки Казахстана;
+10 спрашивает что делать; +10 публикация свежая; +10 есть Kaspi/Halyk/ЧСИ/1414; +10 прямо просит помощь/контакты.
-50 новость/вакансия/мем/реклама; -50 публикация юриста/конкурента; -40 не относится к Казахстану;
-30 общая дискуссия без личной проблемы; -100 мошенничество/угрозы/запрос незаконного способа скрыть имущество.

decision: 0-69 -> skip, 70-89 -> manual_review, 90-100 -> auto_publish.

Верни JSON строго в этой форме:
{
  "relevant": true,
  "decision": "auto_publish|manual_review|skip",
  "language": "ru|kk",
  "problem_type": "account_arrest|vehicle_ban|property_arrest|exit_ban|executive_inscription|salary_withholding|paid_but_not_removed|unknown",
  "lead_score": 0,
  "urgency": "low|medium|high",
  "kazakhstan_probability": 0,
  "risk_flags": [],
  "comment": "",
  "reason": ""
}"""

REQUIRED_FIELDS = {
    "relevant", "decision", "language", "problem_type", "lead_score",
    "urgency", "kazakhstan_probability", "risk_flags", "comment", "reason",
}

FALLBACK_RESULT = {
    "relevant": False,
    "decision": "skip",
    "language": "ru",
    "problem_type": "unknown",
    "lead_score": 0,
    "urgency": "low",
    "kazakhstan_probability": 0,
    "risk_flags": ["deepseek_parse_error"],
    "comment": "",
    "reason": "deepseek_parse_error",
}


class DeepSeekClient:
    def __init__(self, api_key: str | None = None):
        self.client = AsyncOpenAI(
            api_key=api_key or config.deepseek_api_key,
            base_url=config.deepseek_base_url,
        )
        # Populated after each analyze_post() call so callers can log/track
        # spend without changing the analyze_post() return contract.
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0}

    async def analyze_post(self, post_text: str, avoid_comments: list[str] | None = None) -> dict:
        user_content = f"Публикация:\n{post_text}"
        if avoid_comments:
            joined = "\n---\n".join(avoid_comments[:5])
            user_content += (
                "\n\nНе повторяй формулировки этих недавних комментариев, "
                f"придумай другую структуру фраз:\n{joined}"
            )

        try:
            response = await self.client.chat.completions.create(
                model=config.deepseek_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                response_format={"type": "json_object"},
                temperature=0.4,
            )
            raw = response.choices[0].message.content
            if response.usage:
                self.last_usage = {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                }
        except Exception:
            logger.exception("deepseek_request_failed")
            return dict(FALLBACK_RESULT)

        return parse_deepseek_json(raw)


def parse_deepseek_json(raw: str | None) -> dict:
    if not raw:
        return dict(FALLBACK_RESULT)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("deepseek_invalid_json")
        return dict(FALLBACK_RESULT)

    if not isinstance(data, dict) or not REQUIRED_FIELDS.issubset(data.keys()):
        logger.warning("deepseek_missing_fields")
        return dict(FALLBACK_RESULT)

    try:
        data["lead_score"] = max(0, min(100, int(data["lead_score"])))
        data["kazakhstan_probability"] = max(0, min(100, int(data["kazakhstan_probability"])))
    except (TypeError, ValueError):
        return dict(FALLBACK_RESULT)

    if data["decision"] not in ("auto_publish", "manual_review", "skip"):
        data["decision"] = "skip"
    if data["language"] not in ("ru", "kk"):
        data["language"] = "ru"
    if not isinstance(data.get("comment"), str):
        data["comment"] = ""
    data["comment"] = data["comment"][:420]

    return data
