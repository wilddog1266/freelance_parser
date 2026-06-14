"""Доменные модели."""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Project:
    """Заказ с freelance.ru."""
    id: str                              # внутренний id заказа (из URL /task/view/ID)
    url: str
    title: str
    description: str = ""
    budget: str | None = None            # человекочитаемый бюджет, напр. "30 000 ₽ / заказ"
    budget_value: int | None = None      # числовое значение бюджета для фильтра (None = неизвестен)
    published_at: str | None = None      # исходная строка времени, напр. "13.06.2026 19:46"
    published_dt: datetime | None = None  # распарсенное время для фильтрации по возрасту
    category: str | None = None
    tags: list[str] = field(default_factory=list)
    is_premium: bool = False              # «Только для Премиум» — без оплаты не открыть
    source: str = "freelance_ru"          # источник заказа: "freelance_ru" | "kwork"

    def search_text(self) -> str:
        """Единый текст заказа для локальной фильтрации по ключевым словам."""
        parts = [self.title, self.description, self.category, " ".join(self.tags)]
        return " ".join(p for p in parts if p)


@dataclass
class Analysis:
    """Результат AI-анализа заказа."""
    score: int                # 0-100
    suitable: bool            # подходит ли в принципе
    summary: str              # краткое резюме
    why_fits: str             # почему подходит
    risks: str                # риски
    complexity: str           # примерная сложность
    price_range: str          # примерная вилка цены
    reject_reason: str = ""   # почему НЕ подходит (обязательно, если suitable=false)
    reply_short: str = ""     # отклик: коротко и по делу (2-3 предложения)
    reply_confident: str = "" # отклик: уверенный, с предложением следующего шага
    reply_expert: str = ""    # отклик: экспертный, с подходом и уточняющими вопросами


@dataclass
class Filters:
    """Пользовательские фильтры (хранятся в SQLite, настраиваются через Telegram)."""
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    min_budget: int = 0       # 0 = без ограничения
    max_budget: int = 0       # 0 = без ограничения
    min_ai_score: int = 70
    updated_at: str | None = None
