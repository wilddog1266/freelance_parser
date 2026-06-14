"""Минимальный HTTP health-сервер для хостингов, которые ждут слушающий порт.

Render (web service) считает сервис живым, только если процесс слушает $PORT и
отвечает на health-check. Бот сам по себе работает через Telegram long polling и
порт не открывает — поэтому здесь поднимается лёгкий aiohttp-сервер в том же
event loop, что и polling. На сам бот это не влияет.

Маршруты:
    GET /         -> 200 OK
    GET /health   -> 200 OK
"""
import logging

from aiohttp import web

logger = logging.getLogger("health")


async def _ok(_request: web.Request) -> web.Response:
    return web.Response(text="OK")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", _ok)
    app.router.add_get("/health", _ok)
    return app


async def start_health_server(port: int) -> web.AppRunner:
    """Запускает health-сервер на 0.0.0.0:port. Возвращает runner для остановки."""
    runner = web.AppRunner(build_app())
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info("Health-сервер слушает :%d (GET / , GET /health)", port)
    return runner
