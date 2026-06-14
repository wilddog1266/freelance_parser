"""Локальная фильтрация заказов ДО обращения к AI.

Главная цель — не тратить токены DeepSeek на заведомо нерелевантные заказы
(нейминг, проекты домов, презентации, рассылки и т.п.). AI вызывается ТОЛЬКО
для заказов, по которым passes() вернул (True, ...).

Порядок проверок:
  1. exclude-слова  -> любое совпадение = мгновенный отказ;
  2. include-слова  -> если список задан, нужно хотя бы одно совпадение;
  3. бюджет         -> проверка min/max (только если бюджет известен).
"""
import re
from functools import lru_cache

from ..models import Filters, Project


@lru_cache(maxsize=512)
def _compile(keyword: str) -> re.Pattern:
    # Совпадение по границе слова (без \b, чтобы корректно работали цифры/дефисы):
    # "ai" не совпадёт внутри "email", "1с" совпадёт в "1С-разработка".
    return re.compile(rf"(?<!\w){re.escape(keyword.lower())}(?!\w)", re.IGNORECASE)


def _contains(text: str, keyword: str) -> bool:
    keyword = keyword.strip()
    if not keyword:
        return False
    return _compile(keyword).search(text) is not None


def passes(
    project: Project,
    filters: Filters,
    *,
    include_enabled: bool = True,
    exclude_enabled: bool = True,
    budget_enabled: bool = True,
) -> tuple[bool, str, str]:
    """Возвращает (прошёл_ли, категория_отказа, причина).

    Категория ∈ {"exclude", "include", "budget"} при отказе, иначе "".
    Каждую проверку можно отключить флагом — отключённая проверка не отсеивает.
    """
    text = project.search_text().lower()

    # 1. Стоп-слова — приоритетный отказ.
    if exclude_enabled:
        for kw in filters.exclude_keywords:
            if _contains(text, kw):
                return False, "exclude", f"стоп-слово «{kw}»"

    # 2. Ключевые слова — нужно хотя бы одно (если список не пуст).
    if include_enabled and filters.include_keywords:
        matched = [kw for kw in filters.include_keywords if _contains(text, kw)]
        if not matched:
            return False, "include", "нет ни одного ключевого слова"

    # 3. Бюджет. Неизвестный бюджет («обсуждается») не блокирует — пусть решает AI.
    if budget_enabled:
        bv = project.budget_value
        if bv is not None:
            if filters.min_budget and bv < filters.min_budget:
                return False, "budget", f"бюджет {bv} < минимума {filters.min_budget}"
            if filters.max_budget and bv > filters.max_budget:
                return False, "budget", f"бюджет {bv} > максимума {filters.max_budget}"

    return True, "", "прошёл локальный фильтр"
