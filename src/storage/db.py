"""SQLite-хранилище: фильтры пользователя + просмотренные/проанализированные заказы."""
import os
import sqlite3
from datetime import datetime, timedelta

from ..config import (
    DEFAULT_EXCLUDE_KEYWORDS,
    DEFAULT_INCLUDE_KEYWORDS,
    DEFAULT_MAX_BUDGET,
    DEFAULT_MIN_AI_SCORE,
    DEFAULT_MIN_BUDGET,
    config,
)
from ..models import Analysis, Filters, Project


def get_storage():
    """Фабрика хранилища: PostgreSQL при заданном DATABASE_URL, иначе SQLite.

    Публичный интерфейс обоих классов идентичен, поэтому остальной код не знает,
    какая БД используется. Локально (без DATABASE_URL) поведение не меняется.
    """
    url = (config.database_url or "").strip()
    if url.startswith(("postgres://", "postgresql://")):
        # Ленивый импорт: psycopg нужен только для PostgreSQL-режима.
        from .postgres import PostgresStorage
        return PostgresStorage(url)
    return Storage(config.db_path)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS filters (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    include_keywords TEXT NOT NULL DEFAULT '',
    exclude_keywords TEXT NOT NULL DEFAULT '',
    min_budget       INTEGER NOT NULL DEFAULT 0,
    max_budget       INTEGER NOT NULL DEFAULT 0,
    min_ai_score     INTEGER NOT NULL DEFAULT 70,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_projects (
    id            TEXT PRIMARY KEY,   -- составной uid: "<source>:<site_id>"
    source        TEXT NOT NULL DEFAULT 'freelance_ru',
    url           TEXT NOT NULL,
    title         TEXT,
    description   TEXT,
    budget        TEXT,
    budget_value  INTEGER,
    category      TEXT,
    tags          TEXT,
    published_at  TEXT,
    -- результат локального фильтра
    passed_filter INTEGER DEFAULT 0,
    filter_reason TEXT,
    -- результат AI-анализа (NULL, если AI не вызывался)
    score           INTEGER,
    suitable        INTEGER,
    summary         TEXT,
    why_fits        TEXT,
    reject_reason   TEXT,
    risks           TEXT,
    complexity      TEXT,
    price_range     TEXT,
    reply_draft     TEXT,            -- legacy, оставлен для совместимости
    reply_short     TEXT,
    reply_confident TEXT,
    reply_expert    TEXT,
    sent            INTEGER DEFAULT 0,
    hidden          INTEGER NOT NULL DEFAULT 0,   -- 🙈 «Неинтересно»
    created_at      TEXT NOT NULL
);
"""

_KW_SEP = ", "


def _join(keywords: list[str]) -> str:
    return _KW_SEP.join(k.strip() for k in keywords if k.strip())


def _split(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _row_to_project(row: sqlite3.Row) -> Project:
    """Восстанавливает Project из строки seen_projects (id — составной uid)."""
    tags = [t.strip() for t in (row["tags"] or "").split(",") if t.strip()]
    return Project(
        id=row["id"], url=row["url"], title=row["title"] or "",
        description=row["description"] or "", budget=row["budget"],
        budget_value=row["budget_value"], published_at=row["published_at"],
        category=row["category"], tags=tags, source=row["source"],
    )


class Storage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # check_same_thread=False: бот вызывает pipeline в отдельном потоке (to_thread)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self._migrate()
        self._seed_filters()

    @staticmethod
    def _uid(source: str, site_id: str) -> str:
        """Глобальный ключ заказа: уникален между источниками (source, id)."""
        return f"{source}:{site_id}"

    def _migrate(self) -> None:
        """Однократная миграция старой БД под мульти-источники.

        Добавляет колонку source и переводит старые «голые» id заказов
        freelance.ru в составной формат "freelance_ru:<id>" — чтобы они не
        считались новыми и не рассылались повторно.
        """
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(seen_projects)")}
        if "source" not in cols:
            self.conn.execute(
                "ALTER TABLE seen_projects ADD COLUMN "
                "source TEXT NOT NULL DEFAULT 'freelance_ru'"
            )
            self.conn.execute(
                "UPDATE seen_projects SET id = 'freelance_ru:' || id "
                "WHERE instr(id, ':') = 0"
            )
            self.conn.commit()

        # Колонки для строгого скоринга, 3 откликов и скрытия заказов.
        added = False
        new_cols = {
            "reject_reason": "TEXT",
            "reply_short": "TEXT",
            "reply_confident": "TEXT",
            "reply_expert": "TEXT",
            "hidden": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, decl in new_cols.items():
            if name not in cols:
                self.conn.execute(f"ALTER TABLE seen_projects ADD COLUMN {name} {decl}")
                added = True
        if added:
            self.conn.commit()

    # ---------- фильтры ----------

    def _seed_filters(self) -> None:
        row = self.conn.execute("SELECT 1 FROM filters WHERE id = 1").fetchone()
        if row is None:
            self.save_filters(
                Filters(
                    include_keywords=DEFAULT_INCLUDE_KEYWORDS,
                    exclude_keywords=DEFAULT_EXCLUDE_KEYWORDS,
                    min_budget=DEFAULT_MIN_BUDGET,
                    max_budget=DEFAULT_MAX_BUDGET,
                    min_ai_score=DEFAULT_MIN_AI_SCORE,
                )
            )

    def get_filters(self) -> Filters:
        row = self.conn.execute("SELECT * FROM filters WHERE id = 1").fetchone()
        if row is None:
            return Filters()
        return Filters(
            include_keywords=_split(row["include_keywords"]),
            exclude_keywords=_split(row["exclude_keywords"]),
            min_budget=row["min_budget"],
            max_budget=row["max_budget"],
            min_ai_score=row["min_ai_score"],
            updated_at=row["updated_at"],
        )

    def save_filters(self, filters: Filters) -> None:
        self.conn.execute(
            """
            INSERT INTO filters
                (id, include_keywords, exclude_keywords, min_budget, max_budget,
                 min_ai_score, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                include_keywords = excluded.include_keywords,
                exclude_keywords = excluded.exclude_keywords,
                min_budget       = excluded.min_budget,
                max_budget       = excluded.max_budget,
                min_ai_score     = excluded.min_ai_score,
                updated_at       = excluded.updated_at
            """,
            (
                _join(filters.include_keywords),
                _join(filters.exclude_keywords),
                filters.min_budget,
                filters.max_budget,
                filters.min_ai_score,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()

    def update_filter_field(self, field: str, value) -> None:
        """Точечное обновление одного поля фильтра (для режима редактирования)."""
        allowed = {
            "include_keywords", "exclude_keywords",
            "min_budget", "max_budget", "min_ai_score",
        }
        if field not in allowed:
            raise ValueError(f"Недопустимое поле фильтра: {field}")
        if field in ("include_keywords", "exclude_keywords") and isinstance(value, list):
            value = _join(value)
        self.conn.execute(
            f"UPDATE filters SET {field} = ?, updated_at = ? WHERE id = 1",
            (value, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def reset_filters(self) -> None:
        self.conn.execute("DELETE FROM filters WHERE id = 1")
        self.conn.commit()
        self._seed_filters()

    # ---------- заказы ----------

    def is_seen(self, project: Project) -> bool:
        uid = self._uid(project.source, project.id)
        cur = self.conn.execute(
            "SELECT 1 FROM seen_projects WHERE id = ?", (uid,)
        )
        return cur.fetchone() is not None

    def save_project(
        self,
        project: Project,
        passed_filter: bool,
        filter_reason: str = "",
        analysis: Analysis | None = None,
        sent: bool = False,
    ) -> None:
        """Сохраняет заказ с результатом локального фильтра и (опц.) AI-анализа."""
        self.conn.execute(
            """
            INSERT INTO seen_projects
                (id, source, url, title, description, budget, budget_value, category, tags,
                 published_at, passed_filter, filter_reason,
                 score, suitable, summary, why_fits, reject_reason, risks, complexity,
                 price_range, reply_short, reply_confident, reply_expert, sent, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                passed_filter   = excluded.passed_filter,
                filter_reason   = excluded.filter_reason,
                score           = excluded.score,
                suitable        = excluded.suitable,
                summary         = excluded.summary,
                why_fits        = excluded.why_fits,
                reject_reason   = excluded.reject_reason,
                risks           = excluded.risks,
                complexity      = excluded.complexity,
                price_range     = excluded.price_range,
                reply_short     = excluded.reply_short,
                reply_confident = excluded.reply_confident,
                reply_expert    = excluded.reply_expert,
                sent            = excluded.sent
            """,
            (
                self._uid(project.source, project.id), project.source,
                project.url, project.title, project.description,
                project.budget, project.budget_value, project.category,
                _KW_SEP.join(project.tags) if project.tags else None,
                project.published_at,
                int(passed_filter), filter_reason,
                analysis.score if analysis else None,
                int(analysis.suitable) if analysis else None,
                analysis.summary if analysis else None,
                analysis.why_fits if analysis else None,
                analysis.reject_reason if analysis else None,
                analysis.risks if analysis else None,
                analysis.complexity if analysis else None,
                analysis.price_range if analysis else None,
                analysis.reply_short if analysis else None,
                analysis.reply_confident if analysis else None,
                analysis.reply_expert if analysis else None,
                int(sent),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()

    def get_recent_analyzed(
        self, days: int = 7, limit: int = 5, offset: int = 0, min_score: int = 0
    ) -> list[sqlite3.Row]:
        """Подходящие заказы за N дней (suitable + score >= порог), по убыванию оценки."""
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        cur = self.conn.execute(
            """
            SELECT * FROM seen_projects
            WHERE suitable = 1 AND score >= ? AND created_at >= ?
                  AND COALESCE(hidden, 0) = 0
            ORDER BY score DESC, created_at DESC
            LIMIT ? OFFSET ?
            """,
            (min_score, since, limit, offset),
        )
        return cur.fetchall()

    def count_recent_analyzed(self, days: int = 7, min_score: int = 0) -> int:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        cur = self.conn.execute(
            """
            SELECT COUNT(*) AS c FROM seen_projects
            WHERE suitable = 1 AND score >= ? AND created_at >= ?
                  AND COALESCE(hidden, 0) = 0
            """,
            (min_score, since),
        )
        return cur.fetchone()["c"]

    def get_projects_for_recheck(self, days: int = 7) -> list[Project]:
        """Заказы за N дней, пригодные для повторной AI-оценки.

        Исключает: скрытые (hidden=1), premium/платные (по filter_reason),
        без title/url, а также всё старше N дней (по created_at). Заказы не
        парсятся заново — берётся уже сохранённое описание.
        """
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        rows = self.conn.execute(
            """
            SELECT * FROM seen_projects
            WHERE created_at >= ?
                  AND COALESCE(hidden, 0) = 0
                  AND title IS NOT NULL AND title != ''
                  AND url IS NOT NULL AND url != ''
                  AND COALESCE(filter_reason, '') NOT LIKE 'premium%'
            ORDER BY created_at DESC
            """,
            (since,),
        ).fetchall()
        return [_row_to_project(r) for r in rows]

    def update_project_analysis(
        self, uid: str, analysis: Analysis | None,
        passed_filter: bool = True, filter_reason: str = "",
    ) -> None:
        """Обновляет результат анализа существующего заказа (переоценка).

        analysis=None — заказ не прошёл текущий локальный фильтр: помечаем
        suitable=0, чтобы он исчез из «Подходящих». Не вставляет новые строки,
        не трогает created_at/sent/hidden.
        """
        if analysis is None:
            self.conn.execute(
                """
                UPDATE seen_projects
                SET passed_filter = ?, filter_reason = ?, suitable = 0
                WHERE id = ?
                """,
                (int(passed_filter), filter_reason, uid),
            )
        else:
            self.conn.execute(
                """
                UPDATE seen_projects
                SET passed_filter = ?, filter_reason = ?,
                    score = ?, suitable = ?, summary = ?, why_fits = ?,
                    reject_reason = ?, risks = ?, complexity = ?, price_range = ?,
                    reply_short = ?, reply_confident = ?, reply_expert = ?
                WHERE id = ?
                """,
                (
                    int(passed_filter), filter_reason,
                    analysis.score, int(analysis.suitable), analysis.summary,
                    analysis.why_fits, analysis.reject_reason, analysis.risks,
                    analysis.complexity, analysis.price_range,
                    analysis.reply_short, analysis.reply_confident,
                    analysis.reply_expert, uid,
                ),
            )
        self.conn.commit()

    def hide_project(self, uid: str) -> None:
        """Помечает заказ как «Неинтересно» — он исчезает из подходящих."""
        self.conn.execute(
            "UPDATE seen_projects SET hidden = 1 WHERE id = ?", (uid,)
        )
        self.conn.commit()

    def count_seen_projects(self) -> int:
        """Сколько всего заказов в истории (для экрана очистки)."""
        return self.conn.execute("SELECT COUNT(*) AS c FROM seen_projects").fetchone()["c"]

    def clear_seen_projects(self) -> int:
        """Удаляет всю историю найденных/проанализированных заказов (для тестов).

        Фильтры не трогаются. После очистки пайплайн обработает заказы заново.
        Возвращает число удалённых записей.
        """
        cur = self.conn.execute("SELECT COUNT(*) AS c FROM seen_projects")
        count = cur.fetchone()["c"]
        self.conn.execute("DELETE FROM seen_projects")
        self.conn.commit()
        return count

    def get_project(self, project_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM seen_projects WHERE id = ?", (project_id,)
        ).fetchone()

    def update_replies(
        self, uid: str, short: str, confident: str, expert: str,
        reject_reason: str = "",
    ) -> None:
        """Кэширует сгенерированные 3 варианта отклика (для кнопки в боте)."""
        self.conn.execute(
            """
            UPDATE seen_projects
            SET reply_short = ?, reply_confident = ?, reply_expert = ?,
                reject_reason = COALESCE(NULLIF(?, ''), reject_reason)
            WHERE id = ?
            """,
            (short, confident, expert, reject_reason, uid),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
