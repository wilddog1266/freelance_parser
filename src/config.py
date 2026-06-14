"""Конфигурация проекта: env-переменные, профиль разработчика, дефолтные фильтры."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "да")


@dataclass
class Config:
    # DeepSeek (API совместим с OpenAI)
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    # Telegram
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Поведение
    pages_to_parse: int = int(os.getenv("PAGES_TO_PARSE", "5"))
    max_age_hours: int = int(os.getenv("MAX_AGE_HOURS", "168"))
    request_delay: float = float(os.getenv("REQUEST_DELAY", "1.5"))

    # Локальные фильтры ДО AI. DeepSeek дёшев — по умолчанию отдаём в AI почти всё,
    # отсекая локально только стоп-слова (include/budget выключены).
    local_include_filter_enabled: bool = _bool(os.getenv("LOCAL_INCLUDE_FILTER_ENABLED"), False)
    local_exclude_filter_enabled: bool = _bool(os.getenv("LOCAL_EXCLUDE_FILTER_ENABLED"), True)
    local_budget_filter_enabled: bool = _bool(os.getenv("LOCAL_BUDGET_FILTER_ENABLED"), False)
    fetch_details: bool = _bool(os.getenv("FETCH_DETAILS"), True)
    dry_run: bool = _bool(os.getenv("DRY_RUN"), False)
    db_path: str = os.getenv("DB_PATH", "data/parser.db")
    # Если задан DATABASE_URL (postgres://...), хранилище переключается на
    # PostgreSQL (Render и т.п.). Локально без этой переменной — SQLite.
    database_url: str = os.getenv("DATABASE_URL", "")

    # HTTP health-сервер (нужен Render, который ждёт слушающий порт).
    port: int = int(os.getenv("PORT", "10000"))

    # Kwork (второй источник — раздел «Биржа»)
    kwork_enabled: bool = _bool(os.getenv("KWORK_ENABLED"), True)
    kwork_base_url: str = os.getenv("KWORK_BASE_URL", "https://kwork.ru")
    kwork_exchange_url: str = os.getenv("KWORK_EXCHANGE_URL", "https://kwork.ru/projects")
    kwork_pages_to_parse: int = int(os.getenv("KWORK_PAGES_TO_PARSE", "3"))
    kwork_cookies: str = os.getenv("KWORK_COOKIES", "")

    def validate(self) -> list[str]:
        """Возвращает список проблем конфигурации (пустой = всё ок)."""
        problems = []
        if not self.deepseek_api_key:
            problems.append("DEEPSEEK_API_KEY не задан")
        if not self.telegram_bot_token:
            problems.append("TELEGRAM_BOT_TOKEN не задан")
        if not self.telegram_chat_id:
            problems.append("TELEGRAM_CHAT_ID не задан")
        return problems


config = Config()


# Дефолтные фильтры — записываются в SQLite при первом запуске, дальше
# пользователь меняет их через Telegram-бота. Ориентированы на ТИП задачи,
# а не на конкретный стек (реализовать можно на чём угодно).
DEFAULT_INCLUDE_KEYWORDS = [
    "сайт", "лендинг", "landing", "визитк", "crm", "админк", "dashboard",
    "личный кабинет", "панель управления", "внутренний инструмент",
    "бот", "telegram", "телеграм", "mini app", "мини-приложение",
    "парсер", "парсинг", "scraping", "автоматизаци", "интеграци", "api",
    "ai", "gpt", "нейросет", "mvp", "веб-приложение", "web-приложение",
]
DEFAULT_EXCLUDE_KEYWORDS = [
    "1с", "1c", "битрикс", "bitrix", "wordpress", "вордпресс",
    "casino", "казино", "adult", "gambling", "ставки", "беттинг",
]
DEFAULT_MIN_BUDGET = 0      # 0 = без ограничения
DEFAULT_MAX_BUDGET = 0      # 0 = без ограничения
DEFAULT_MIN_AI_SCORE = 70


# Профиль разработчика — на его основе AI решает, насколько заказ хорош.
# Ориентирован на тип задач, т.к. разработчик работает в AI-assisted формате
# и стек реализации может быть любым.
PROFILE = """
Я full-stack разработчик в AI-assisted формате (vibe-coding): могу реализовать
задачу на разном стеке, поэтому ориентируюсь на ТИП задачи, а не на конкретную
технологию.

ПОДХОДЯЩИЕ типы задач:
- лендинги, сайты-визитки, небольшие бизнес-сайты, веб-приложения, MVP;
- CRM, админ-панели, личные кабинеты, внутренние инструменты, дашборды;
- автоматизация бизнес-процессов;
- Telegram-боты и Telegram Mini Apps;
- парсеры и сбор данных;
- интеграции (API, сторонние сервисы);
- интеграции с AI / LLM.

НЕ ПОДХОДЯТ (снижай оценку или отклоняй):
- требуется 1С;
- Bitrix / WordPress как обязательная платформа;
- требуется уровень Senior и большой коммерческий опыт;
- нужен офис или полная занятость (нужна удалёнка / частичная занятость);
- бюджет явно мал для объёма работы;
- заказ мутный, токсичный, без конкретики, похож на развод или инфоцыганство.
"""
