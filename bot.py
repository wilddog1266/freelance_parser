"""Точка входа Telegram-бота: настройка фильтров и просмотр заказов.

Запуск:
    python bot.py

Бот работает в режиме long polling. Для регулярного парсинга по расписанию
используй main.py (cron) — он читает те же фильтры из SQLite.
"""
import asyncio
import logging

from src.telegram.bot import run_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except (KeyboardInterrupt, SystemExit) as e:
        logging.getLogger("bot").info("Остановлено: %s", e)
