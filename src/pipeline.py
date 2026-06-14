"""Пайплайн обработки заказов.

ПОРЯДОК (ключевое требование — AI только после локального фильтра):

    парсер фида
        -> свежесть (max_age_hours)
        -> локальный фильтр по данным карточки   [filtering/local_filter.py]
        -> enrich (полное описание со страницы заказа)
        -> локальный фильтр повторно (по полному тексту)
        ===> ТОЛЬКО ЗДЕСЬ вызывается DeepSeek (ai.analyze)
        -> сохранение в SQLite
        -> отправка в Telegram (если suitable и score >= min_ai_score)

Заказы, не прошедшие локальный фильтр, помечаются в БД как просмотренные
(без AI-анализа), чтобы не обрабатывать их повторно и не тратить токены.
"""
import logging
from datetime import datetime, timedelta

from .ai.analyzer import get_provider
from .config import config
from .filtering.local_filter import passes
from .models import Project
from .parser.freelance_ru import FreelanceRuParser
from .parser.kwork import KworkParser
from .storage.db import get_storage
from .telegram.notifier import TelegramNotifier

logger = logging.getLogger("pipeline")


def _is_fresh(project: Project, max_age_hours: int) -> bool:
    if project.published_dt is None:
        return True  # не смогли распарсить дату — лучше проверить
    return project.published_dt >= datetime.now() - timedelta(hours=max_age_hours)


def run_pipeline(dry_run: bool | None = None) -> dict:
    """Один проход пайплайна. Возвращает статистику для отчёта (в т.ч. боту)."""
    if dry_run is None:
        dry_run = config.dry_run

    stats = {
        "total": 0, "total_freelance_ru": 0, "total_kwork": 0,
        "already_seen": 0, "new": 0, "premium_skipped": 0, "old": 0,
        "filtered_include": 0, "filtered_exclude": 0, "filtered_budget": 0,
        "analyzed": 0, "matched": 0, "sent": 0, "errors": 0,
    }

    storage = get_storage()
    filters = storage.get_filters()
    fl_parser = FreelanceRuParser(
        request_delay=config.request_delay,
        fetch_details=config.fetch_details,
    )
    # Парсеры по источнику — нужны для enrich() конкретного заказа.
    parsers = {"freelance_ru": fl_parser}
    kwork_parser = None
    if config.kwork_enabled:
        kwork_parser = KworkParser(
            base_url=config.kwork_base_url,
            exchange_url=config.kwork_exchange_url,
            request_delay=config.request_delay,
            cookies=config.kwork_cookies,
        )
        parsers["kwork"] = kwork_parser
    ai = get_provider()

    notifier = None
    can_notify = not dry_run and not config.validate()
    if can_notify:
        notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)

    try:
        projects = fl_parser.fetch_projects(pages=config.pages_to_parse)
        stats["total_freelance_ru"] = len(projects)
        if kwork_parser:
            kwork_projects = kwork_parser.fetch_projects(pages=config.kwork_pages_to_parse)
            stats["total_kwork"] = len(kwork_projects)
            projects += kwork_projects
        stats["total"] = len(projects)

        for project in projects:
            if storage.is_seen(project):
                stats["already_seen"] += 1
                continue
            stats["new"] += 1

            # 0. Premium/платные задания нельзя открыть без оплаты — не тратим
            # на них AI и не отправляем. Помечаем seen, чтобы не обрабатывать повторно.
            if project.is_premium:
                stats["premium_skipped"] += 1
                logger.info("Пропуск premium-заказа #%s: %s", project.id, project.title)
                storage.save_project(project, passed_filter=False,
                                     filter_reason="premium (платный)")
                continue

            # 1. Свежесть.
            if not _is_fresh(project, config.max_age_hours):
                stats["old"] += 1
                storage.save_project(project, passed_filter=False,
                                     filter_reason="старше окна")
                continue

            # 2. Предварительный локальный фильтр по данным карточки.
            #    Состав проверок настраивается флагами (include/exclude/budget).
            ok, cat, reason = passes(
                project, filters,
                include_enabled=config.local_include_filter_enabled,
                exclude_enabled=config.local_exclude_filter_enabled,
                budget_enabled=config.local_budget_filter_enabled,
            )
            if not ok:
                stats[f"filtered_{cat}"] += 1
                logger.info("Фильтр (%s) отсеял #%s: %s", cat, project.id, project.title)
                storage.save_project(project, passed_filter=False, filter_reason=reason)
                continue

            # 3. Полное описание -> повторный фильтр (в теле мог быть стоп-сигнал).
            parsers[project.source].enrich(project)
            ok, cat, reason = passes(
                project, filters,
                include_enabled=config.local_include_filter_enabled,
                exclude_enabled=config.local_exclude_filter_enabled,
                budget_enabled=config.local_budget_filter_enabled,
            )
            if not ok:
                stats[f"filtered_{cat}"] += 1
                logger.info("Фильтр (%s) отсеял #%s после enrich", cat, project.id)
                storage.save_project(project, passed_filter=False, filter_reason=reason)
                continue

            # 4. AI вызывается ТОЛЬКО здесь — для прошедших локальный фильтр.
            logger.info("AI-анализ #%s: %s", project.id, project.title)
            analysis = ai.analyze(project)
            if analysis is None:
                stats["errors"] += 1
                logger.warning("AI не вернул результат для #%s — повтор в след. раз", project.id)
                continue  # не сохраняем как seen, чтобы попробовать снова

            stats["analyzed"] += 1
            matched = analysis.suitable and analysis.score >= filters.min_ai_score
            logger.info("Оценка #%s: %d/100 (порог %d) -> %s. Причина: %s",
                        project.id, analysis.score, filters.min_ai_score,
                        "ПОДХОДИТ" if matched else "мимо",
                        analysis.reject_reason or "—")

            sent = False
            if matched:
                stats["matched"] += 1
                if notifier:
                    sent = notifier.send_project(project, analysis)
                    if sent:
                        stats["sent"] += 1
                elif dry_run:
                    logger.info("[dry-run] отправил бы в Telegram #%s", project.id)

            storage.save_project(project, passed_filter=True, filter_reason=reason,
                                 analysis=analysis, sent=sent)
    finally:
        fl_parser.close()
        if kwork_parser:
            kwork_parser.close()
        if hasattr(ai, "close"):
            ai.close()
        if notifier:
            notifier.close()
        storage.close()

    logger.info(
        "Готово. Всего:%d (fl:%d kwork:%d) новых:%d (ранее:%d) premium:%d старых:%d "
        "фильтр[incl:%d excl:%d budget:%d] AI:%d подходящих:%d отправлено:%d ошибок:%d",
        stats["total"], stats["total_freelance_ru"], stats["total_kwork"],
        stats["new"], stats["already_seen"], stats["premium_skipped"], stats["old"],
        stats["filtered_include"], stats["filtered_exclude"], stats["filtered_budget"],
        stats["analyzed"], stats["matched"], stats["sent"], stats["errors"],
    )
    return stats


def recheck_recent(days: int = 7) -> dict:
    """Переоценивает уже найденные заказы за N дней под ТЕКУЩИЕ фильтры и AI-промпт.

    НЕ парсит ленту заново и НЕ отправляет ничего в Telegram — только заново
    прогоняет сохранённые заказы через локальный фильтр и AI и обновляет оценки
    в SQLite. Нужно, когда после смены порога/стоп-слов/промпта старые оценки
    в «Подходящих за неделю» устарели.
    """
    stats = {"checked": 0, "still_matched": 0, "now_rejected": 0, "errors": 0}

    storage = get_storage()
    ai = get_provider()
    try:
        filters = storage.get_filters()
        projects = storage.get_projects_for_recheck(days=days)
        logger.info("Переоценка: к проверке %d заказ(ов) за %d дн.", len(projects), days)

        for project in projects:
            # 1. Текущий локальный фильтр. Не прошёл — AI не зовём, гасим suitable.
            ok, cat, reason = passes(
                project, filters,
                include_enabled=config.local_include_filter_enabled,
                exclude_enabled=config.local_exclude_filter_enabled,
                budget_enabled=config.local_budget_filter_enabled,
            )
            if not ok:
                stats["checked"] += 1
                stats["now_rejected"] += 1
                storage.update_project_analysis(project.id, None,
                                                passed_filter=False, filter_reason=reason)
                logger.info("Переоценка #%s: отсеян фильтром (%s)", project.id, cat)
                continue

            # 2. Повторный AI-анализ по сохранённому описанию.
            analysis = ai.analyze(project)
            if analysis is None:
                stats["errors"] += 1
                logger.warning("Переоценка #%s: AI не ответил — оставляю как было", project.id)
                continue

            stats["checked"] += 1
            storage.update_project_analysis(project.id, analysis,
                                            passed_filter=True, filter_reason=reason)
            matched = analysis.suitable and analysis.score >= filters.min_ai_score
            if matched:
                stats["still_matched"] += 1
            else:
                stats["now_rejected"] += 1
            logger.info("Переоценка #%s: %d/100 (порог %d) -> %s. Причина: %s",
                        project.id, analysis.score, filters.min_ai_score,
                        "ПОДХОДИТ" if matched else "мимо", analysis.reject_reason or "—")
    finally:
        if hasattr(ai, "close"):
            ai.close()
        storage.close()

    logger.info(
        "Переоценка завершена: проверено %d, подходящих %d, неподходящих %d, ошибок %d",
        stats["checked"], stats["still_matched"], stats["now_rejected"], stats["errors"],
    )
    return stats
