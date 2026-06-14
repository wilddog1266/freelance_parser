"""AI-анализ заказов.

Провайдер вынесен за абстракцию AIProvider — чтобы заменить DeepSeek на любой
другой LLM (OpenAI, локальную модель и т.п.) достаточно добавить новый класс
и поменять get_provider(). Сейчас по умолчанию используется DeepSeek
(его API совместим с форматом OpenAI chat/completions).
"""
import json
import logging
import re
from abc import ABC, abstractmethod

import httpx

from ..config import PROFILE, config
from ..models import Analysis, Project

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — ассистент фрилансера-разработчика, который оценивает заказы с бирж
(freelance.ru, Kwork). Тебе дают профиль исполнителя и текст заказа. Оцени честно
и строго, насколько это РЕАЛЬНАЯ РАЗРАБОТКА, и подготовь материалы для отклика.

ГЛАВНОЕ ПРАВИЛО: высокий score — только задачам, где есть реальная разработка,
автоматизация или техническая реализация. Если в заказе встречаются слова
Telegram, AI, сайт, но по смыслу это НЕ разработка — высокий score не ставить.

Шкала score (0-100):

80-100 — реальная разработка:
- разработка сайта / лендинга / веб-приложения;
- Telegram-бот; Telegram Mini App;
- CRM / админка / личный кабинет;
- парсер / сбор данных;
- автоматизация бизнес-процессов;
- интеграция API; AI/LLM-интеграции;
- MVP продукта;
- доработка существующего кода, если задача понятна.

50-79 — потенциально техническая, но со спорными моментами:
- задача техническая, но плохо/мутно описана;
- слишком общий запрос, нужны уточнения;
- неясный бюджет;
- частично подходит.

0-49 — НЕ разработка / не подходит:
- рассылки, размещение сообщений, постинг в группах/каналах;
- отзывы, лидогенерация без разработки, "привести клиентов", заявки/лиды;
- монтаж видео, AI-видео без разработки;
- дизайн логотипов/баннеров/Figma без разработки;
- копирайтинг; SEO-продвижение без технической части;
- ручная работа, продажи, менеджерские задачи;
- ставки/казино/adult;
- 1С / Bitrix / WordPress как ОБЯЗАТЕЛЬНАЯ платформа без альтернативы;
- требуется Senior/офис/полная занятость; мутный/токсичный заказ.

Примеры: «Размещение сообщений в Telegram-группах» → низкий; «AI-видео» →
низкий/средний если нет разработки; «Заявки/лиды для сервиса» → низкий;
«Продвижение сайта» → низкий, если нет технической разработки.

suitable=true только если score достаточно высок и это действительно разработка.
Если suitable=false — поле reject_reason ОБЯЗАТЕЛЬНО (коротко, по сути).

Сгенерируй 3 варианта отклика. Требования ко всем: естественно, как живой
исполнитель; без шаблонности; не обещать лишнего; не писать «у меня большой
опыт» и «готов взяться» без конкретики; НЕ упоминать AI-assisted/vibe-coding.
- reply_short: 2-3 предложения, коротко и по делу.
- reply_confident: увереннее — показать понимание задачи и предложить следующий шаг.
- reply_expert: сильнейший — кратко разобрать задачу, предложить подход к
  реализации и задать 1-2 уточняющих вопроса.

Отвечай СТРОГО валидным JSON без markdown-обёртки, со следующими полями:
{
  "score": <int 0-100>,
  "suitable": <true|false>,
  "summary": "<2-3 предложения: суть заказа>",
  "why_fits": "<почему подходит или не подходит профилю>",
  "reject_reason": "<если suitable=false — почему отклонён; иначе пустая строка>",
  "risks": "<основные риски: неясность ТЗ, бюджет, заказчик и т.п.>",
  "complexity": "<низкая/средняя/высокая + пояснение>",
  "price_range": "<примерная вилка цены в рублях с обоснованием>",
  "reply_short": "<короткий отклик>",
  "reply_confident": "<уверенный отклик>",
  "reply_expert": "<экспертный отклик>"
}
Все текстовые поля — на русском языке."""


def _build_user_prompt(project: Project) -> str:
    parts = [
        "=== ПРОФИЛЬ ИСПОЛНИТЕЛЯ ===",
        PROFILE.strip(),
        "",
        "=== ЗАКАЗ ===",
        f"Название: {project.title}",
    ]
    if project.category:
        parts.append(f"Категория: {project.category}")
    if project.tags:
        parts.append(f"Навыки/теги: {', '.join(project.tags)}")
    if project.budget:
        parts.append(f"Бюджет: {project.budget}")
    if project.published_at:
        parts.append(f"Опубликован: {project.published_at}")
    parts.append("")
    parts.append("Описание:")
    parts.append(project.description or "(описание отсутствует)")
    return "\n".join(parts)


class AIProvider(ABC):
    @abstractmethod
    def analyze(self, project: Project) -> Analysis | None:
        ...


class DeepSeekProvider(AIProvider):
    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.model = model
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.client = httpx.Client(timeout=90)

    def analyze(self, project: Project) -> Analysis | None:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(project)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.4,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = self.client.post(self.endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError) as e:
            logger.error("Ошибка DeepSeek API для заказа %s: %s", project.id, e)
            return None

        data = _parse_json(content)
        if data is None:
            logger.error("Не удалось распарсить ответ AI для заказа %s", project.id)
            return None

        try:
            return Analysis(
                score=int(data.get("score", 0)),
                suitable=bool(data.get("suitable", False)),
                summary=_text(data.get("summary")),
                why_fits=_text(data.get("why_fits")),
                reject_reason=_text(data.get("reject_reason")),
                risks=_text(data.get("risks")),
                complexity=_text(data.get("complexity")),
                price_range=_text(data.get("price_range")),
                reply_short=_text(data.get("reply_short")),
                reply_confident=_text(data.get("reply_confident")),
                reply_expert=_text(data.get("reply_expert")),
            )
        except (ValueError, TypeError) as e:
            logger.error("Некорректные поля в ответе AI для %s: %s", project.id, e)
            return None

    def close(self) -> None:
        self.client.close()


def _text(value) -> str:
    """Нормализует поле ответа AI в строку. Список (напр. risks: [...]) -> '; '."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


def _parse_json(content: str) -> dict | None:
    """Парсит JSON, при необходимости вытаскивая его из markdown-обёртки."""
    content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def get_provider() -> AIProvider:
    """Фабрика AI-провайдера. Замени тело, чтобы подключить другой LLM."""
    return DeepSeekProvider(
        api_key=config.deepseek_api_key,
        base_url=config.deepseek_base_url,
        model=config.deepseek_model,
    )
