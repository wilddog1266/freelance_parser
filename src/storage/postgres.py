"""PostgreSQL-хранилище: полное зеркало SQLite Storage для деплоя (Render).

Активируется автоматически фабрикой get_storage(), когда задан DATABASE_URL
(postgres://... | postgresql://...). Публичный интерфейс совпадает со Storage:
те же методы с теми же сигнатурами, а строки результатов — dict (row["col"]),
как и у sqlite3.Row, поэтому остальной код менять не нужно.

Совместимость с бизнес-логикой:
- created_at/updated_at хранятся как ISO-строки (TEXT) — сравнения вида
  `created_at >= since` работают лексикографически, как в SQLite;
- булевы поля (passed_filter/suitable/sent/hidden) — INTEGER 0/1, как в SQLite;
- INSERT ... ON CONFLICT ... DO UPDATE SET ... = excluded.* поддерживается PG.
"""
import logging
from datetime import datetime, timedelta

import psycopg
from psycopg.rows import dict_row

from ..config import (
    DEFAULT_EXCLUDE_KEYWORDS,
    DEFAULT_INCLUDE_KEYWORDS,
    DEFAULT_MAX_BUDGET,
    DEFAULT_MIN_AI_SCORE,
    DEFAULT_MIN_BUDGET,
)
from ..models import Analysis, Filters, Project
# Переиспользуем общие хелперы из SQLite-модуля, чтобы не дублировать логику.
from .db import _KW_SEP, _join, _row_to_project, _split

logger = logging.getLogger("storage.pg")

# Каждый CREATE выполняется отдельным execute(): psycopg3 (расширенный протокол)
# не допускает несколько команд в одном вызове.
_DDL = [
    """
    CREATE TABLE IF NOT EXISTS filters (
        id               INTEGER PRIMARY KEY CHECK (id = 1),
        include_keywords TEXT NOT NULL DEFAULT '',
        exclude_keywords TEXT NOT NULL DEFAULT '',
        min_budget       INTEGER NOT NULL DEFAULT 0,
        max_budget       INTEGER NOT NULL DEFAULT 0,
        min_ai_score     INTEGER NOT NULL DEFAULT 70,
        updated_at       TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS seen_projects (
        id            TEXT PRIMARY KEY,
        source        TEXT NOT NULL DEFAULT 'freelance_ru',
        url           TEXT NOT NULL,
        title         TEXT,
        description   TEXT,
        budget        TEXT,
        budget_value  INTEGER,
        category      TEXT,
        tags          TEXT,
        published_at  TEXT,
        passed_filter INTEGER DEFAULT 0,
        filter_reason TEXT,
        score           INTEGER,
        suitable        INTEGER,
        summary         TEXT,
        why_fits        TEXT,
        reject_reason   TEXT,
        risks           TEXT,
        complexity      TEXT,
        price_range     TEXT,
        reply_draft     TEXT,
        reply_short     TEXT,
        reply_confident TEXT,
        reply_expert    TEXT,
        sent            INTEGER DEFAULT 0,
        hidden          INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL
    )
    """,
]


class PostgresStorage:
    def __init__(self, dsn: str):
        # libpq понимает обе схемы, но нормализуем для единообразия.
        if dsn.startswith("postgres://"):
            dsn = "postgresql://" + dsn[len("postgres://"):]
        # autocommit=True: код исторически вызывает commit() после записей —
        # при autocommit эти вызовы безопасны (no-op), а чтение не держит
        # «idle in transaction» на долгоживущем боте.
        self.conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        self._create_schema()
        self._seed_filters()
        logger.info("PostgreSQL-хранилище инициализировано")

    @staticmethod
    def _uid(source: str, site_id: str) -> str:
        return f"{source}:{site_id}"

    def _create_schema(self) -> None:
        for ddl in _DDL:
            self.conn.execute(ddl)

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
            VALUES (1, %s, %s, %s, %s, %s, %s)
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

    def update_filter_field(self, field: str, value) -> None:
        allowed = {
            "include_keywords", "exclude_keywords",
            "min_budget", "max_budget", "min_ai_score",
        }
        if field not in allowed:
            raise ValueError(f"Недопустимое поле фильтра: {field}")
        if field in ("include_keywords", "exclude_keywords") and isinstance(value, list):
            value = _join(value)
        self.conn.execute(
            f"UPDATE filters SET {field} = %s, updated_at = %s WHERE id = 1",
            (value, datetime.now().isoformat(timespec="seconds")),
        )

    def reset_filters(self) -> None:
        self.conn.execute("DELETE FROM filters WHERE id = 1")
        self._seed_filters()

    # ---------- заказы ----------

    def is_seen(self, project: Project) -> bool:
        uid = self._uid(project.source, project.id)
        row = self.conn.execute(
            "SELECT 1 FROM seen_projects WHERE id = %s", (uid,)
        ).fetchone()
        return row is not None

    def save_project(
        self,
        project: Project,
        passed_filter: bool,
        filter_reason: str = "",
        analysis: Analysis | None = None,
        sent: bool = False,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO seen_projects
                (id, source, url, title, description, budget, budget_value, category, tags,
                 published_at, passed_filter, filter_reason,
                 score, suitable, summary, why_fits, reject_reason, risks, complexity,
                 price_range, reply_short, reply_confident, reply_expert, sent, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s)
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

    def get_recent_analyzed(
        self, days: int = 7, limit: int = 5, offset: int = 0, min_score: int = 0
    ) -> list[dict]:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        return self.conn.execute(
            """
            SELECT * FROM seen_projects
            WHERE suitable = 1 AND score >= %s AND created_at >= %s
                  AND COALESCE(hidden, 0) = 0
            ORDER BY score DESC, created_at DESC
            LIMIT %s OFFSET %s
            """,
            (min_score, since, limit, offset),
        ).fetchall()

    def count_recent_analyzed(self, days: int = 7, min_score: int = 0) -> int:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS c FROM seen_projects
            WHERE suitable = 1 AND score >= %s AND created_at >= %s
                  AND COALESCE(hidden, 0) = 0
            """,
            (min_score, since),
        ).fetchone()
        return row["c"]

    def get_projects_for_recheck(self, days: int = 7) -> list[Project]:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        rows = self.conn.execute(
            """
            SELECT * FROM seen_projects
            WHERE created_at >= %s
                  AND COALESCE(hidden, 0) = 0
                  AND title IS NOT NULL AND title != ''
                  AND url IS NOT NULL AND url != ''
                  AND COALESCE(filter_reason, '') NOT LIKE 'premium%%'
            ORDER BY created_at DESC
            """,
            (since,),
        ).fetchall()
        return [_row_to_project(r) for r in rows]

    def update_project_analysis(
        self, uid: str, analysis: Analysis | None,
        passed_filter: bool = True, filter_reason: str = "",
    ) -> None:
        if analysis is None:
            self.conn.execute(
                """
                UPDATE seen_projects
                SET passed_filter = %s, filter_reason = %s, suitable = 0
                WHERE id = %s
                """,
                (int(passed_filter), filter_reason, uid),
            )
        else:
            self.conn.execute(
                """
                UPDATE seen_projects
                SET passed_filter = %s, filter_reason = %s,
                    score = %s, suitable = %s, summary = %s, why_fits = %s,
                    reject_reason = %s, risks = %s, complexity = %s, price_range = %s,
                    reply_short = %s, reply_confident = %s, reply_expert = %s
                WHERE id = %s
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

    def hide_project(self, uid: str) -> None:
        self.conn.execute(
            "UPDATE seen_projects SET hidden = 1 WHERE id = %s", (uid,)
        )

    def count_seen_projects(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS c FROM seen_projects"
        ).fetchone()["c"]

    def clear_seen_projects(self) -> int:
        count = self.count_seen_projects()
        self.conn.execute("DELETE FROM seen_projects")
        return count

    def get_project(self, project_id: str) -> dict | None:
        return self.conn.execute(
            "SELECT * FROM seen_projects WHERE id = %s", (project_id,)
        ).fetchone()

    def update_replies(
        self, uid: str, short: str, confident: str, expert: str,
        reject_reason: str = "",
    ) -> None:
        self.conn.execute(
            """
            UPDATE seen_projects
            SET reply_short = %s, reply_confident = %s, reply_expert = %s,
                reject_reason = COALESCE(NULLIF(%s, ''), reject_reason)
            WHERE id = %s
            """,
            (short, confident, expert, reject_reason, uid),
        )

    def close(self) -> None:
        self.conn.close()
