"""Headless-вход для cron: один проход пайплайна без интерактива.

Сам по себе НЕ настраивает фильтры — они задаются через Telegram-бота (bot.py)
и читаются из SQLite. Здесь только: парсинг -> локальный фильтр -> AI -> Telegram.

Запуск:
    python main.py            # один проход
    python main.py --dry-run  # анализ без отправки в Telegram

Cron (каждые 30 минут):
    */30 * * * * cd /путь/к/проекту && /путь/к/.venv/bin/python main.py >> data/run.log 2>&1
"""
import argparse
import logging

from src.config import config
from src.pipeline import run_pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


def main() -> None:
    ap = argparse.ArgumentParser(description="Парсер заказов freelance.ru с AI-фильтром")
    ap.add_argument("--dry-run", action="store_true",
                    help="анализировать, но не отправлять в Telegram")
    args = ap.parse_args()
    run_pipeline(dry_run=args.dry_run or config.dry_run)


if __name__ == "__main__":
    main()
