"""Отправка подходящих заказов в Telegram через Bot API."""
import logging
from html import escape

import httpx

from ..models import Analysis, Project

logger = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.client = httpx.Client(timeout=30)

    def send_project(self, project: Project, analysis: Analysis) -> bool:
        text = self._format(project, analysis)
        return self._send(text, self._keyboard(project))

    @staticmethod
    def _keyboard(project: Project) -> dict:
        """Те же действия, что и в боте: callback'и совпадают по uid (source:id)."""
        uid = f"{project.source}:{project.id}"
        return {
            "inline_keyboard": [
                [{"text": "💬 Сгенерировать ответ", "callback_data": f"gen:{uid}"}],
                [{"text": "🙈 Неинтересно", "callback_data": f"hide:{uid}"}],
                [{"text": "🔗 Перейти к заказу", "url": project.url}],
            ]
        }

    def _send(self, text: str, reply_markup: dict | None = None) -> bool:
        if len(text) > TELEGRAM_LIMIT:
            text = text[: TELEGRAM_LIMIT - 1] + "…"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            resp = self.client.post(self.api_url, json=payload)
            resp.raise_for_status()
            return True
        except httpx.HTTPError as e:
            logger.error("Ошибка отправки в Telegram: %s", e)
            return False

    @staticmethod
    def _format(project: Project, analysis: Analysis) -> str:
        e = escape
        lines = [
            f"🆕 <b>{e(project.title)}</b>  ⭐ <b>{analysis.score}/100</b>",
        ]
        if project.budget:
            lines.append(f"💰 {e(project.budget)}")
        if project.category:
            lines.append(f"🗂 {e(project.category)}")
        if project.published_at:
            lines.append(f"🕒 {e(project.published_at)}")
        lines += [
            "",
            f"📝 {e(analysis.summary)}",
            "",
            f"✅ <b>Почему подходит:</b> {e(analysis.why_fits)}",
            f"⚠️ <b>Риски:</b> {e(analysis.risks)}",
            f"🔧 <b>Сложность:</b> {e(analysis.complexity)}",
            f"💵 <b>Вилка:</b> {e(analysis.price_range)}",
        ]
        if analysis.reply_short:
            lines += [
                "",
                "💬 <b>Черновик отклика:</b>",
                f"<blockquote>{e(analysis.reply_short)}</blockquote>",
                "<i>Полные 3 варианта — кнопка «Сгенерировать ответ».</i>",
            ]
        return "\n".join(lines)

    def close(self) -> None:
        self.client.close()
