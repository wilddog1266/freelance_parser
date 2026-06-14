"""Парсер заказов с Kwork — раздел «Биржа» (https://kwork.ru/projects).

Важно: парсим именно ЗАКАЗЫ ПОКУПАТЕЛЕЙ с биржи, а не кворки исполнителей.

Как устроено (проверено июнь 2026):
- Страница биржи отдаётся server-side, без авторизации и без JavaScript.
- Список заказов лежит в инлайн-JSON состояния страницы:
      ... "pagination":{"current_page":1,"data":[ {<заказ>}, ... ]} ...
  Каждый <заказ> — объект с полями id, name, description, priceLimit,
  date_active, category_id и т.д.
- Пагинация: ?page=N (12 заказов на страницу).
- URL заказа: https://kwork.ru/projects/{id}/view

Интерфейс совместим с FreelanceRuParser (fetch_projects / enrich / close),
поэтому пайплайн обрабатывает оба источника единообразно. Описание уже есть
в ленте, поэтому enrich() для Kwork — no-op.

Если Kwork сменит вёрстку/структуру — правки локализованы в этом файле.
"""
import json
import logging
import time
from datetime import datetime

import httpx

from ..models import Project

logger = logging.getLogger(__name__)

SOURCE = "kwork"


class KworkParser:
    def __init__(
        self,
        base_url: str = "https://kwork.ru",
        exchange_url: str = "https://kwork.ru/projects",
        request_delay: float = 1.5,
        cookies: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.exchange_url = exchange_url
        self.request_delay = request_delay
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9",
        }
        # Cookies — fallback на случай, если Kwork когда-нибудь закроет биржу
        # за авторизацией. Сейчас не требуются (KWORK_COOKIES можно оставить пустым).
        if cookies:
            headers["Cookie"] = cookies
        self.client = httpx.Client(headers=headers, timeout=25, follow_redirects=True)

    # ---------- публичный API (совместим с FreelanceRuParser) ----------

    def fetch_projects(self, pages: int = 3) -> list[Project]:
        """Парсит N страниц биржи и возвращает заказы (описание уже включено)."""
        projects: list[Project] = []
        seen_ids: set[str] = set()
        for page in range(1, pages + 1):
            html = self._get(f"{self.exchange_url}?page={page}")
            if not html:
                continue
            raw_items = self._extract_items(html)
            logger.info("Kwork страница %d: найдено %d заказов", page, len(raw_items))
            if not raw_items:
                break  # дальше пусто — нет смысла листать
            for item in raw_items:
                project = self._to_project(item)
                if project and project.id not in seen_ids:
                    seen_ids.add(project.id)
                    projects.append(project)
            if page < pages:
                time.sleep(self.request_delay)
        return projects

    def enrich(self, project: Project) -> Project:
        """Описание уже приходит в ленте — отдельная детализация не нужна."""
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
            logger.warning("Kwork: ошибка запроса %s: %s", url, e)
            return None

    @staticmethod
    def _extract_items(html: str) -> list[dict]:
        """Достаёт массив заказов из инлайн-JSON: ...\"pagination\":{...\"data\":[...]}."""
        marker = '"pagination":{'
        i = html.find(marker)
        if i == -1:
            return []
        j = html.find('"data":[', i)
        if j == -1:
            return []
        start = j + len('"data":')
        depth = 0
        end = None
        for k in range(start, len(html)):
            c = html[k]
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    end = k + 1
                    break
        if end is None:
            return []
        try:
            data = json.loads(html[start:end])
        except json.JSONDecodeError as e:
            logger.warning("Kwork: не удалось разобрать JSON ленты: %s", e)
            return []
        return data if isinstance(data, list) else []

    def _to_project(self, item: dict) -> Project | None:
        raw_id = item.get("id")
        if raw_id is None:
            return None
        project_id = str(raw_id)
        title = (item.get("name") or "").strip()
        url = f"{self.base_url}/projects/{project_id}/view"
        description = (item.get("description") or "").strip()

        budget, budget_value = self._parse_budget(item.get("priceLimit"))
        published_at, published_dt = self._parse_date(item.get("date_active"))

        return Project(
            id=project_id,
            url=url,
            title=title,
            description=description,
            budget=budget,
            budget_value=budget_value,
            published_at=published_at,
            published_dt=published_dt,
            category=None,
            tags=[],
            source=SOURCE,
        )

    @staticmethod
    def _parse_budget(raw) -> tuple[str | None, int | None]:
        """priceLimit ('500.00' / 500 / None) -> ('500 ₽', 500)."""
        if raw in (None, "", 0, "0", "0.00"):
            return "Не указан (договорной)", None
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            return "Не указан (договорной)", None
        return f"{value} ₽", value

    @staticmethod
    def _parse_date(raw: str | None) -> tuple[str | None, datetime | None]:
        """date_active вида '2026-06-14 21:26:57' -> (строка, datetime)."""
        if not raw:
            return None, None
        try:
            return raw, datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return raw, None
