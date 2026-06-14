"""Парсер заказов с freelance.ru.

Структура (проверена на июнь 2026):
- фид:        https://freelance.ru/task?page=N
- карточка:   <article class="task-card">
- ссылка:     <a class="task-card__title-link" href="/task/view/ID">
- описание:   <p class="task-card__desc">
- категория:  <span class="task-chip task-chip--cat">
- время:      <span class="task-card__foot-item" title="13.06.2026 19:46">
- бюджет:     <div class="task-card__budget">
- детально:   <section class="tcard tv-hero"> (полное описание), <div class="tv-detail__block"> (навыки)

Если freelance.ru изменит вёрстку — правки локализованы в этом файле.
"""
import logging
import re
import time
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from ..models import Project

logger = logging.getLogger(__name__)

BASE_URL = "https://freelance.ru"
FEED_URL = BASE_URL + "/task"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}


class FreelanceRuParser:
    def __init__(self, request_delay: float = 1.5, fetch_details: bool = True):
        self.request_delay = request_delay
        self.fetch_details = fetch_details
        self.client = httpx.Client(headers=HEADERS, timeout=25, follow_redirects=True)

    # ---------- публичный API ----------

    def fetch_projects(self, pages: int = 2) -> list[Project]:
        """Парсит N страниц фида и возвращает список заказов (без детализации)."""
        projects: list[Project] = []
        seen_ids: set[str] = set()
        for page in range(1, pages + 1):
            html = self._get(f"{FEED_URL}?page={page}")
            if not html:
                continue
            page_projects = self._parse_feed(html)
            logger.info("Страница %d: найдено %d заказов", page, len(page_projects))
            for p in page_projects:
                if p.id not in seen_ids:
                    seen_ids.add(p.id)
                    projects.append(p)
            if page < pages:
                time.sleep(self.request_delay)
        return projects

    def enrich(self, project: Project) -> Project:
        """Догружает полное описание и навыки со страницы заказа."""
        if not self.fetch_details:
            return project
        html = self._get(project.url)
        if html:
            self._parse_detail(html, project)
        time.sleep(self.request_delay)
        return project

    def close(self) -> None:
        self.client.close()

    # ---------- внутреннее ----------

    def _get(self, url: str) -> str | None:
        try:
            resp = self.client.get(url)
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPError as e:
            logger.warning("Ошибка запроса %s: %s", url, e)
            return None

    def _parse_feed(self, html: str) -> list[Project]:
        soup = BeautifulSoup(html, "html.parser")
        projects = []
        for card in soup.select("article.task-card"):
            project = self._parse_card(card)
            if project:
                projects.append(project)
        return projects

    def _parse_card(self, card) -> Project | None:
        link = card.select_one("a.task-card__title-link")
        if not link or not link.get("href"):
            return None
        href = link["href"]
        url = href if href.startswith("http") else BASE_URL + href
        project_id = self._extract_id(href)
        title = link.get("title") or link.get_text(strip=True)

        desc_el = card.select_one("p.task-card__desc")
        description = desc_el.get_text(" ", strip=True) if desc_el else ""

        cat_el = card.select_one("span.task-chip--cat")
        category = cat_el.get_text(strip=True) if cat_el else None

        budget = self._parse_budget(card.select_one("div.task-card__budget"))
        budget_value = self._extract_budget_value(budget)

        published_at, published_dt = self._parse_date(card)

        # «Только для Премиум» — задание без оплаты не открыть (класс на карточке
        # task-card--premium + бейдж task-badge--premium).
        is_premium = (
            "task-card--premium" in (card.get("class") or [])
            or card.select_one(".task-badge--premium") is not None
        )

        return Project(
            id=project_id,
            url=url,
            title=title,
            description=description,
            budget=budget,
            budget_value=budget_value,
            published_at=published_at,
            published_dt=published_dt,
            category=category,
            tags=[category] if category else [],
            is_premium=is_premium,
        )

    def _parse_detail(self, html: str, project: Project) -> None:
        soup = BeautifulSoup(html, "html.parser")

        # Полное описание: блок tv-hero, текст после заголовка h1.
        hero = soup.select_one("section.tv-hero")
        if hero:
            h1 = hero.find("h1")
            if h1:
                h1.extract()
            # топовая мета-строка не нужна в описании
            top = hero.select_one(".tv-hero__top")
            if top:
                top.extract()
            full_desc = hero.get_text("\n", strip=True)
            if full_desc:
                project.description = full_desc

        # Навыки/теги из блоков tv-detail.
        tags = [
            chip.get_text(strip=True)
            for chip in soup.select("div.tv-detail__block span.task-chip")
        ]
        if tags:
            # категория + навыки, без дублей, с сохранением порядка
            merged = ([project.category] if project.category else []) + tags
            project.tags = list(dict.fromkeys(t for t in merged if t))

        # Уточнённый бюджет со страницы заказа.
        budget_el = soup.select_one(".tv-meta-item__val--budget")
        detailed_budget = self._parse_budget(budget_el)
        if detailed_budget:
            project.budget = detailed_budget
            project.budget_value = self._extract_budget_value(detailed_budget)

    @staticmethod
    def _extract_id(href: str) -> str:
        m = re.search(r"/(\d+)(?:[/?#]|$)", href)
        return m.group(1) if m else href

    @staticmethod
    def _parse_budget(el) -> str | None:
        if not el:
            return None
        text = el.get_text(" ", strip=True)
        if not text:
            return None
        # "Обсуждается индивидуально" / "По договорённости" — бюджет не задан
        if el.select_one(".cost-not-set") or "обсужда" in text.lower():
            return "Не указан (обсуждается)"
        return text

    @staticmethod
    def _extract_budget_value(budget: str | None) -> int | None:
        """Числовое значение бюджета для фильтра. None, если бюджет не указан.

        "3 500 ₽ / заказ" -> 3500; "от 10 000 ₽" -> 10000;
        "Не указан (обсуждается)" -> None.
        """
        if not budget:
            return None
        # склеиваем разряды: "3 500" (в т.ч. неразрывные пробелы) -> "3500"
        joined = re.sub(r"(?<=\d)\s(?=\d)", "", budget)
        m = re.search(r"\d{3,}", joined)  # >=3 цифр, чтобы не цеплять "1 заказ", "1 час"
        return int(m.group(0)) if m else None

    @staticmethod
    def _parse_date(card) -> tuple[str | None, datetime | None]:
        """Точное время лежит в атрибуте title у элемента с иконкой часов."""
        for item in card.select("span.task-card__foot-item"):
            if item.select_one("i.fa-clock-o"):
                raw = item.get("title")  # "13.06.2026 19:46"
                if raw:
                    try:
                        return raw, datetime.strptime(raw, "%d.%m.%Y %H:%M")
                    except ValueError:
                        pass
                return item.get_text(strip=True), None  # "7 часов назад"
        return None, None
