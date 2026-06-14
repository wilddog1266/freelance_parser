"""Telegram-бот (aiogram 3): управление фильтрами и просмотр найденных заказов.

Функционал:
- /start          -> главное меню (inline-кнопки);
- Настроить фильтры  -> пошаговый FSM (5 шагов), сохранение в SQLite;
- Редактировать      -> точечное изменение полей фильтра / сброс;
- Найденные за неделю -> список из SQLite с пагинацией и кнопками под заказом;
- Сгенерировать ответ -> показать сохранённый reply_draft или сгенерировать через AI;
- Запустить проверку  -> запуск pipeline (в отдельном потоке, чтобы не блокировать loop).

Фильтры здесь ТОЛЬКО настраиваются и сохраняются. Применяет их пайплайн
(src/pipeline.py): локальная фильтрация -> AI только для прошедших.
"""
import asyncio
import logging
import re
from datetime import datetime
from html import escape as esc

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..ai.analyzer import get_provider
from ..config import config
from ..models import Filters, Project
from ..pipeline import recheck_recent, run_pipeline
from ..health import start_health_server
from ..storage.db import Storage, get_storage

logger = logging.getLogger("bot")

PAGE_SIZE = 5
WEEK_DAYS = 7
DEFAULT_INTERVAL_MIN = 5

SOURCE_LABELS = {"freelance_ru": "Freelance.ru", "kwork": "Kwork"}


def source_label(source: str | None) -> str:
    return SOURCE_LABELS.get(source or "freelance_ru", source or "—")

dp = Dispatcher()


# ======================= состояние автомониторинга (в памяти процесса) =======================

class Monitoring:
    task: "asyncio.Task | None" = None
    enabled: bool = False
    interval_min: int = DEFAULT_INTERVAL_MIN
    last_run_at: "datetime | None" = None
    last_stats: "dict | None" = None


async def monitoring_loop() -> None:
    """Фоновый цикл: каждые N минут гоняет pipeline в отдельном потоке."""
    while Monitoring.enabled:
        try:
            stats = await asyncio.to_thread(run_pipeline, False)
            Monitoring.last_run_at = datetime.now()
            Monitoring.last_stats = stats
            logger.info("Мониторинг: проход завершён, подходящих %d", stats.get("matched", 0))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — цикл не должен падать из-за разовой ошибки
            logger.exception("Мониторинг: ошибка прохода")
        # сон порциями, чтобы остановка срабатывала быстро
        for _ in range(Monitoring.interval_min * 60):
            if not Monitoring.enabled:
                break
            await asyncio.sleep(1)


# ======================= FSM-состояния =======================

class Setup(StatesGroup):
    include = State()
    exclude = State()
    min_budget = State()
    max_budget = State()
    min_score = State()


class Edit(StatesGroup):
    include = State()
    exclude = State()
    budget_min = State()
    budget_max = State()
    score = State()


# ======================= ограничение доступа =======================

class AccessMiddleware(BaseMiddleware):
    """MVP — один пользователь: пускаем только TELEGRAM_CHAT_ID (если задан)."""

    def __init__(self, allowed_chat_id: str):
        self.allowed = str(allowed_chat_id) if allowed_chat_id else ""

    async def __call__(self, handler, event, data):
        if self.allowed:
            chat_id = None
            if isinstance(event, Message):
                chat_id = event.chat.id
            elif isinstance(event, CallbackQuery) and event.message:
                chat_id = event.message.chat.id
            if chat_id is not None and str(chat_id) != self.allowed:
                return None
        return await handler(event, data)


# ======================= клавиатуры =======================

def main_menu_kb():
    b = InlineKeyboardBuilder()
    b.button(text="📅 Подходящие за неделю", callback_data="week:0")
    b.button(text="⚙️ Настроить фильтры", callback_data="menu:setup")
    b.button(text="✏️ Редактировать фильтры", callback_data="menu:edit")
    b.button(text="🚀 Запустить проверку сейчас", callback_data="menu:run")
    b.button(text="♻️ Переоценить заказы", callback_data="recheck:start")
    b.button(text="▶️ Запустить мониторинг", callback_data="mon:start")
    b.button(text="⏹ Остановить мониторинг", callback_data="mon:stop")
    b.button(text="📊 Статус мониторинга", callback_data="mon:status")
    b.button(text="⏱ Интервал мониторинга", callback_data="mon:interval")
    b.button(text="🗑 Очистить историю", callback_data="menu:clear")
    b.button(text="❓ Помощь", callback_data="menu:help")
    b.adjust(1)
    return b.as_markup()


def recheck_offer_kb():
    b = InlineKeyboardBuilder()
    b.button(text="♻️ Переоценить заказы", callback_data="recheck:start")
    b.button(text="Не сейчас", callback_data="recheck:skip")
    b.adjust(1)
    return b.as_markup()


def clear_confirm_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🗑 Да, очистить", callback_data="clear:yes")
    b.button(text="↩️ Отмена", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


def interval_menu_kb():
    b = InlineKeyboardBuilder()
    for minutes in (1, 5, 10, 15, 30):
        b.button(text=f"{minutes} мин", callback_data=f"mon:setint:{minutes}")
    b.button(text="🏠 Назад", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


def back_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🏠 Главное меню", callback_data="menu:main")
    return b.as_markup()


def edit_menu_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🔑 Изменить ключевые слова", callback_data="edit:include")
    b.button(text="🚫 Изменить стоп-слова", callback_data="edit:exclude")
    b.button(text="💰 Изменить бюджет", callback_data="edit:budget")
    b.button(text="⭐ Изменить AI Score", callback_data="edit:score")
    b.button(text="♻️ Сбросить фильтры", callback_data="edit:reset")
    b.button(text="🏠 Назад", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


def order_kb(project_id: str, url: str):
    b = InlineKeyboardBuilder()
    b.button(text="💬 Сгенерировать ответ", callback_data=f"gen:{project_id}")
    b.button(text="🙈 Неинтересно", callback_data=f"hide:{project_id}")
    b.button(text="🔗 Перейти к заказу", url=url)
    b.adjust(1)
    return b.as_markup()


def nav_kb(page: int, pages: int):
    b = InlineKeyboardBuilder()
    if page > 0:
        b.button(text="⬅️ Назад", callback_data=f"week:{page - 1}")
    if page + 1 < pages:
        b.button(text="➡️ Далее", callback_data=f"week:{page + 1}")
    b.button(text="🏠 Меню", callback_data="menu:main")
    b.adjust(2)
    return b.as_markup()


# ======================= помощники =======================

def parse_keywords(text: str) -> list[str]:
    if text.strip() in ("-", ""):
        return []
    return [p.strip() for p in text.split(",") if p.strip()]


def parse_int(text: str) -> int | None:
    cleaned = re.sub(r"\s", "", text)
    return int(cleaned) if cleaned.isdigit() else None


def format_filters(f: Filters) -> str:
    inc = ", ".join(f.include_keywords) or "—"
    exc = ", ".join(f.exclude_keywords) or "—"
    return (
        "📋 <b>Текущие фильтры</b>\n\n"
        f"🔑 Ключевые слова:\n{esc(inc)}\n\n"
        f"🚫 Стоп-слова:\n{esc(exc)}\n\n"
        f"💰 Бюджет: от {f.min_budget or '—'} до {f.max_budget or '—'}\n"
        f"⭐ Минимальный AI Score: {f.min_ai_score}\n"
        f"🕒 Обновлено: {f.updated_at or '—'}"
    )


def format_run_stats(stats: dict) -> str:
    return (
        "✅ <b>Проверка завершена</b>\n\n"
        f"• всего на ленте: {stats['total']}\n"
        f"   ↳ Freelance.ru: {stats.get('total_freelance_ru', 0)}\n"
        f"   ↳ Kwork: {stats.get('total_kwork', 0)}\n"
        f"• уже обработано ранее: {stats.get('already_seen', 0)}\n"
        f"• новых заказов: {stats.get('new', 0)}\n"
        f"• premium/платных пропущено: {stats.get('premium_skipped', 0)}\n"
        f"• отсеяно include-фильтром: {stats.get('filtered_include', 0)}\n"
        f"• отсеяно стоп-словами: {stats.get('filtered_exclude', 0)}\n"
        f"• отсеяно по бюджету: {stats.get('filtered_budget', 0)}\n"
        f"• устаревших: {stats['old']}\n"
        f"• проанализировано AI: {stats['analyzed']}\n"
        f"• подходящих: {stats['matched']}\n"
        f"• отправлено в чат: {stats['sent']}\n"
        f"• ошибок: {stats['errors']}"
    )


def format_order(num: int, row) -> str:
    lines = [
        f"<b>{num}. {esc(row['title'] or 'Без названия')}</b>",
        f"⭐ Оценка: {row['score']}/100",
        f"💰 Бюджет: {esc(row['budget'] or '—')}",
        f"📍 Источник: {source_label(row['source'])}",
    ]
    if row["category"]:
        lines.append(f"🗂 {esc(row['category'])}")
    return "\n".join(lines)


def row_to_project(row) -> Project:
    tags = [t.strip() for t in (row["tags"] or "").split(",") if t.strip()]
    return Project(
        id=row["id"], url=row["url"], title=row["title"] or "",
        description=row["description"] or "", budget=row["budget"],
        budget_value=row["budget_value"], published_at=row["published_at"],
        category=row["category"], tags=tags, source=row["source"],
    )


def _analyze(project: Project):
    """Синхронный вызов AI — запускается через asyncio.to_thread."""
    provider = get_provider()
    try:
        return provider.analyze(project)
    finally:
        if hasattr(provider, "close"):
            provider.close()


# ======================= команды =======================

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "📌 <b>Главное меню</b>\n\nЯ ищу заказы на freelance.ru, фильтрую их "
        "локально и отдаю на AI-анализ только подходящие. Выбери действие:",
        reply_markup=main_menu_kb(),
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_kb())


# ======================= меню (callbacks) =======================

@dp.callback_query(F.data == "menu:main")
async def cb_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("📌 <b>Главное меню</b>", reply_markup=main_menu_kb())
    await cb.answer()


@dp.callback_query(F.data == "menu:help")
async def cb_help(cb: CallbackQuery):
    await cb.message.answer(
        "❓ <b>Помощь</b>\n\n"
        "• <b>Настроить фильтры</b> — задать ключевые/стоп-слова, бюджет и порог AI "
        "(пошагово).\n"
        "• <b>Редактировать</b> — изменить отдельные поля или сбросить.\n"
        "• <b>Найденные за неделю</b> — заказы, прошедшие фильтр и AI, по убыванию оценки.\n"
        "• <b>Сгенерировать ответ</b> — готовый текст отклика (берётся из базы или "
        "генерируется AI).\n"
        "• <b>Запустить проверку</b> — разовый прогон парсера прямо сейчас.\n\n"
        "AI вызывается только для заказов, прошедших локальный фильтр — токены не тратятся "
        "на нерелевантное.",
        reply_markup=back_kb(),
    )
    await cb.answer()


@dp.callback_query(F.data == "menu:run")
async def cb_run(cb: CallbackQuery):
    await cb.answer("Запускаю проверку…")
    msg = await cb.message.answer("🚀 Проверка запущена, это займёт примерно минуту…")
    try:
        stats = await asyncio.to_thread(run_pipeline, False)
    except Exception as e:  # noqa: BLE001 — показать пользователю любую ошибку прогона
        logger.exception("Ошибка пайплайна")
        await msg.edit_text(f"❌ Ошибка при проверке: {esc(str(e))}", reply_markup=back_kb())
        return
    await msg.edit_text(format_run_stats(stats), reply_markup=back_kb())


# ======================= переоценка найденных заказов =======================

def format_recheck_stats(stats: dict) -> str:
    return (
        "♻️ <b>Переоценка завершена</b>\n\n"
        f"• проверено: {stats.get('checked', 0)}\n"
        f"• осталось подходящих: {stats.get('still_matched', 0)}\n"
        f"• стало неподходящими: {stats.get('now_rejected', 0)}\n"
        f"• ошибок: {stats.get('errors', 0)}"
    )


async def offer_recheck(message: Message) -> None:
    """Предложить переоценку после изменения фильтров."""
    await message.answer(
        "Фильтры обновлены.\n"
        "Хотите переоценить уже найденные заказы за последние 7 дней?",
        reply_markup=recheck_offer_kb(),
    )


async def _run_recheck(message: Message) -> None:
    """Запускает переоценку в отдельном потоке, чтобы не блокировать бота."""
    msg = await message.answer(
        "♻️ Переоценка запущена, это может занять несколько минут…"
    )
    try:
        stats = await asyncio.to_thread(recheck_recent, WEEK_DAYS)
    except Exception as e:  # noqa: BLE001 — показать пользователю любую ошибку
        logger.exception("Ошибка переоценки")
        await msg.edit_text(
            f"❌ Ошибка при переоценке: {esc(str(e))}", reply_markup=back_kb()
        )
        return
    await msg.edit_text(format_recheck_stats(stats), reply_markup=back_kb())


@dp.callback_query(F.data == "recheck:start")
async def cb_recheck_start(cb: CallbackQuery):
    await cb.answer("Запускаю переоценку…")
    await _run_recheck(cb.message)


@dp.callback_query(F.data == "recheck:skip")
async def cb_recheck_skip(cb: CallbackQuery):
    await cb.message.answer("Ок, переоценку пропускаем.", reply_markup=back_kb())
    await cb.answer()


# ======================= очистка истории (для тестов) =======================

@dp.callback_query(F.data == "menu:clear")
async def cb_clear_ask(cb: CallbackQuery, storage: Storage):
    total = storage.count_seen_projects()
    await cb.message.answer(
        "🗑 <b>Очистить историю?</b>\n\n"
        f"Будут удалены все обработанные заказы (сейчас в базе: {total}).\n"
        "Фильтры и настройки сохранятся. При следующей проверке заказы "
        "проанализируются заново.\n\n"
        "Действие необратимо.",
        reply_markup=clear_confirm_kb(),
    )
    await cb.answer()


@dp.callback_query(F.data == "clear:yes")
async def cb_clear_do(cb: CallbackQuery, storage: Storage):
    deleted = storage.clear_seen_projects()
    await cb.message.answer(
        f"✅ История очищена. Удалено заказов: {deleted}.",
        reply_markup=back_kb(),
    )
    await cb.answer("История очищена")


# ======================= автомониторинг =======================

@dp.callback_query(F.data == "mon:start")
async def cb_mon_start(cb: CallbackQuery):
    if Monitoring.enabled and Monitoring.task and not Monitoring.task.done():
        await cb.message.answer("⚠️ Мониторинг уже запущен.", reply_markup=back_kb())
        await cb.answer()
        return
    Monitoring.enabled = True
    Monitoring.task = asyncio.create_task(monitoring_loop())
    await cb.message.answer(
        f"▶️ Мониторинг запущен. Интервал: {Monitoring.interval_min} мин.",
        reply_markup=back_kb(),
    )
    await cb.answer()


@dp.callback_query(F.data == "mon:stop")
async def cb_mon_stop(cb: CallbackQuery):
    if not Monitoring.enabled and not (Monitoring.task and not Monitoring.task.done()):
        await cb.message.answer("ℹ️ Мониторинг не запущен.", reply_markup=back_kb())
        await cb.answer()
        return
    Monitoring.enabled = False
    if Monitoring.task:
        Monitoring.task.cancel()
        try:
            await Monitoring.task
        except asyncio.CancelledError:
            pass
        Monitoring.task = None
    await cb.message.answer("⏹ Мониторинг остановлен.", reply_markup=back_kb())
    await cb.answer()


@dp.callback_query(F.data == "mon:status")
async def cb_mon_status(cb: CallbackQuery):
    running = Monitoring.enabled and Monitoring.task and not Monitoring.task.done()
    lines = [
        "📊 <b>Статус мониторинга</b>\n",
        f"• состояние: {'▶️ работает' if running else '⏹ остановлен'}",
        f"• интервал: {Monitoring.interval_min} мин",
    ]
    if Monitoring.last_run_at:
        lines.append(f"• последний проход: {Monitoring.last_run_at:%Y-%m-%d %H:%M:%S}")
        if Monitoring.last_stats:
            s = Monitoring.last_stats
            lines.append(
                f"• итог: новых {s.get('new', 0)}, подходящих {s.get('matched', 0)}, "
                f"отправлено {s.get('sent', 0)}"
            )
    else:
        lines.append("• последний проход: ещё не было")
    await cb.message.answer("\n".join(lines), reply_markup=back_kb())
    await cb.answer()


@dp.callback_query(F.data == "mon:interval")
async def cb_mon_interval(cb: CallbackQuery):
    await cb.message.answer(
        f"⏱ <b>Интервал мониторинга</b>\nТекущий: {Monitoring.interval_min} мин.\n\n"
        "Выбери новый интервал:",
        reply_markup=interval_menu_kb(),
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("mon:setint:"))
async def cb_mon_setint(cb: CallbackQuery):
    minutes = int(cb.data.rsplit(":", 1)[1])
    Monitoring.interval_min = minutes
    note = "" if not (Monitoring.enabled and Monitoring.task) else \
        "\nНовый интервал применится после текущего цикла."
    await cb.message.answer(
        f"✅ Интервал мониторинга: {minutes} мин.{note}",
        reply_markup=back_kb(),
    )
    await cb.answer()


# ======================= просмотр заказов за неделю =======================

@dp.callback_query(F.data.startswith("week:"))
async def cb_week(cb: CallbackQuery, storage: Storage):
    page = int(cb.data.split(":", 1)[1])
    min_score = storage.get_filters().min_ai_score
    total = storage.count_recent_analyzed(WEEK_DAYS, min_score)
    if total == 0:
        await cb.message.answer(
            "За неделю пока нет подходящих заказов.\n"
            "Нажми «🚀 Запустить проверку сейчас».",
            reply_markup=back_kb(),
        )
        await cb.answer()
        return

    rows = storage.get_recent_analyzed(WEEK_DAYS, PAGE_SIZE, page * PAGE_SIZE, min_score)
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    for i, row in enumerate(rows, start=page * PAGE_SIZE + 1):
        await cb.message.answer(format_order(i, row), reply_markup=order_kb(row["id"], row["url"]))
    await cb.message.answer(
        f"📄 Страница {page + 1}/{pages} · всего за неделю: {total}",
        reply_markup=nav_kb(page, pages),
    )
    await cb.answer()


def format_replies(short: str, confident: str, expert: str) -> str:
    blocks = []
    if short:
        blocks.append(f"✏️ <b>Короткий вариант:</b>\n{esc(short)}")
    if confident:
        blocks.append(f"💪 <b>Уверенный вариант:</b>\n{esc(confident)}")
    if expert:
        blocks.append(f"🎓 <b>Экспертный вариант:</b>\n{esc(expert)}")
    return "\n\n".join(blocks) if blocks else "Не удалось получить варианты отклика."


@dp.callback_query(F.data.startswith("gen:"))
async def cb_generate(cb: CallbackQuery, storage: Storage):
    project_id = cb.data.split(":", 1)[1]
    row = storage.get_project(project_id)
    if row is None:
        await cb.answer("Заказ не найден в базе", show_alert=True)
        return

    # Уже сгенерированные варианты берём из кэша (без повторного вызова AI).
    if row["reply_short"]:
        await cb.message.answer(format_replies(
            row["reply_short"], row["reply_confident"], row["reply_expert"]))
        await cb.answer("Показаны сохранённые варианты")
        return

    await cb.answer("Генерирую варианты отклика через AI…")
    analysis = await asyncio.to_thread(_analyze, row_to_project(row))
    if analysis is None:
        await cb.message.answer("❌ Не удалось сгенерировать ответ (ошибка AI).")
        return
    storage.update_replies(
        project_id, analysis.reply_short, analysis.reply_confident,
        analysis.reply_expert, analysis.reject_reason,
    )
    await cb.message.answer(format_replies(
        analysis.reply_short, analysis.reply_confident, analysis.reply_expert))


@dp.callback_query(F.data.startswith("hide:"))
async def cb_hide(cb: CallbackQuery, storage: Storage):
    uid = cb.data.split(":", 1)[1]
    storage.hide_project(uid)
    await cb.answer("Заказ скрыт")
    await cb.message.answer("🙈 Заказ скрыт — больше не появится в «Подходящие за неделю».")


# ======================= настройка фильтров (FSM) =======================

@dp.callback_query(F.data == "menu:setup")
async def cb_setup(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Setup.include)
    await cb.message.answer(
        "⚙️ <b>Настройка фильтров</b> — шаг 1/5\n\n"
        "Введите ключевые слова через запятую (в заказе должно встретиться хотя бы одно):\n\n"
        "Пример: <code>crm, telegram, бот, ai, лендинг</code>\n\n"
        "Отмена — /cancel",
    )
    await cb.answer()


@dp.message(Setup.include)
async def setup_include(message: Message, state: FSMContext):
    await state.update_data(include=parse_keywords(message.text))
    await state.set_state(Setup.exclude)
    await message.answer(
        "Шаг 2/5. Введите стоп-слова через запятую (если встретятся — заказ отбрасывается):\n\n"
        "Пример: <code>1с, bitrix, casino</code>\n\n"
        "Если стоп-слов нет — отправьте «-».",
    )


@dp.message(Setup.exclude)
async def setup_exclude(message: Message, state: FSMContext):
    await state.update_data(exclude=parse_keywords(message.text))
    await state.set_state(Setup.min_budget)
    await message.answer(
        "Шаг 3/5. Минимальный бюджет в рублях (0 — без ограничения):\n\nПример: <code>10000</code>"
    )


@dp.message(Setup.min_budget)
async def setup_min_budget(message: Message, state: FSMContext):
    value = parse_int(message.text)
    if value is None:
        await message.answer("Нужно целое число (например 10000). Попробуйте ещё раз:")
        return
    await state.update_data(min_budget=value)
    await state.set_state(Setup.max_budget)
    await message.answer(
        "Шаг 4/5. Максимальный бюджет в рублях (0 — без ограничения):\n\nПример: <code>100000</code>"
    )


@dp.message(Setup.max_budget)
async def setup_max_budget(message: Message, state: FSMContext):
    value = parse_int(message.text)
    if value is None:
        await message.answer("Нужно целое число (например 100000). Попробуйте ещё раз:")
        return
    await state.update_data(max_budget=value)
    await state.set_state(Setup.min_score)
    await message.answer("Шаг 5/5. Минимальный AI Score (0–100):\n\nПример: <code>70</code>")


@dp.message(Setup.min_score)
async def setup_min_score(message: Message, state: FSMContext, storage: Storage):
    value = parse_int(message.text)
    if value is None or not (0 <= value <= 100):
        await message.answer("Нужно число от 0 до 100. Попробуйте ещё раз:")
        return
    data = await state.get_data()
    filters = Filters(
        include_keywords=data.get("include", []),
        exclude_keywords=data.get("exclude", []),
        min_budget=data.get("min_budget", 0),
        max_budget=data.get("max_budget", 0),
        min_ai_score=value,
    )
    storage.save_filters(filters)
    await state.clear()
    await message.answer("✅ Фильтры сохранены!\n\n" + format_filters(storage.get_filters()))
    await offer_recheck(message)
    await message.answer("📌 <b>Главное меню</b>", reply_markup=main_menu_kb())


# ======================= редактирование фильтров =======================

@dp.callback_query(F.data == "menu:edit")
async def cb_edit(cb: CallbackQuery, state: FSMContext, storage: Storage):
    await state.clear()
    await cb.message.answer(format_filters(storage.get_filters()), reply_markup=edit_menu_kb())
    await cb.answer()


@dp.callback_query(F.data == "edit:reset")
async def cb_edit_reset(cb: CallbackQuery, storage: Storage):
    storage.reset_filters()
    await cb.message.answer("♻️ Фильтры сброшены к значениям по умолчанию.")
    await cb.message.answer(format_filters(storage.get_filters()), reply_markup=edit_menu_kb())
    await offer_recheck(cb.message)
    await cb.answer()


@dp.callback_query(F.data == "edit:include")
async def cb_edit_include(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Edit.include)
    await cb.message.answer("Введите новые ключевые слова через запятую («-» — очистить):")
    await cb.answer()


@dp.message(Edit.include)
async def edit_include(message: Message, state: FSMContext, storage: Storage):
    storage.update_filter_field("include_keywords", parse_keywords(message.text))
    await state.clear()
    await message.answer("✅ Ключевые слова обновлены.")
    await message.answer(format_filters(storage.get_filters()), reply_markup=edit_menu_kb())
    await offer_recheck(message)


@dp.callback_query(F.data == "edit:exclude")
async def cb_edit_exclude(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Edit.exclude)
    await cb.message.answer("Введите новые стоп-слова через запятую («-» — очистить):")
    await cb.answer()


@dp.message(Edit.exclude)
async def edit_exclude(message: Message, state: FSMContext, storage: Storage):
    storage.update_filter_field("exclude_keywords", parse_keywords(message.text))
    await state.clear()
    await message.answer("✅ Стоп-слова обновлены.")
    await message.answer(format_filters(storage.get_filters()), reply_markup=edit_menu_kb())
    await offer_recheck(message)


@dp.callback_query(F.data == "edit:budget")
async def cb_edit_budget(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Edit.budget_min)
    await cb.message.answer("Введите минимальный бюджет (0 — без ограничения):")
    await cb.answer()


@dp.message(Edit.budget_min)
async def edit_budget_min(message: Message, state: FSMContext):
    value = parse_int(message.text)
    if value is None:
        await message.answer("Нужно целое число. Попробуйте ещё раз:")
        return
    await state.update_data(min_budget=value)
    await state.set_state(Edit.budget_max)
    await message.answer("Введите максимальный бюджет (0 — без ограничения):")


@dp.message(Edit.budget_max)
async def edit_budget_max(message: Message, state: FSMContext, storage: Storage):
    value = parse_int(message.text)
    if value is None:
        await message.answer("Нужно целое число. Попробуйте ещё раз:")
        return
    data = await state.get_data()
    storage.update_filter_field("min_budget", data.get("min_budget", 0))
    storage.update_filter_field("max_budget", value)
    await state.clear()
    await message.answer("✅ Бюджет обновлён.")
    await message.answer(format_filters(storage.get_filters()), reply_markup=edit_menu_kb())
    await offer_recheck(message)


@dp.callback_query(F.data == "edit:score")
async def cb_edit_score(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Edit.score)
    await cb.message.answer("Введите минимальный AI Score (0–100):")
    await cb.answer()


@dp.message(Edit.score)
async def edit_score(message: Message, state: FSMContext, storage: Storage):
    value = parse_int(message.text)
    if value is None or not (0 <= value <= 100):
        await message.answer("Нужно число от 0 до 100. Попробуйте ещё раз:")
        return
    storage.update_filter_field("min_ai_score", value)
    await state.clear()
    await message.answer("✅ AI Score обновлён.")
    await message.answer(format_filters(storage.get_filters()), reply_markup=edit_menu_kb())
    await offer_recheck(message)


# ======================= запуск =======================

async def run_bot() -> None:
    if not config.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан в .env")

    bot = Bot(
        config.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    storage = get_storage()
    dp.message.outer_middleware(AccessMiddleware(config.telegram_chat_id))
    dp.callback_query.outer_middleware(AccessMiddleware(config.telegram_chat_id))

    # HTTP health-сервер поднимается рядом с long polling (нужен Render).
    # Оба работают в одном event loop и не мешают друг другу.
    health_runner = await start_health_server(config.port)

    logger.info("Бот запущен (long polling)")
    try:
        await dp.start_polling(bot, storage=storage)
    finally:
        Monitoring.enabled = False
        if Monitoring.task and not Monitoring.task.done():
            Monitoring.task.cancel()
            try:
                await Monitoring.task
            except asyncio.CancelledError:
                pass
        await health_runner.cleanup()
        storage.close()
