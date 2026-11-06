from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, bot_token: str, proxy_url: str | None = None):
        self.bot_token = bot_token
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(45, connect=15), proxy=proxy_url
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def _call(self, method: str, payload: dict) -> dict:
        logger.debug("Calling Telegram Bot API method=%s", method)
        try:
            response = await self.http.post(f"{self.base_url}/{method}", json=payload)
        except httpx.HTTPError as exc:
            detail = str(exc).replace(self.bot_token, "<redacted>")
            raise TelegramError(f"{method}: {detail}") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise TelegramError(
                f"Telegram returned HTTP {response.status_code}"
            ) from exc
        if not response.is_success or not body.get("ok"):
            description = body.get("description", f"HTTP {response.status_code}")
            raise TelegramError(str(description))
        return body

    async def send_message(
        self, chat_id: int, text: str, *, reply_markup: dict | None = None
    ) -> None:
        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await self._call("sendMessage", payload)

    async def get_updates(
        self, offset: int | None = None, timeout: int = 30
    ) -> list[dict]:
        payload: dict = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        body = await self._call("getUpdates", payload)
        return list(body.get("result") or [])

    async def set_webhook(self, url: str, secret: str) -> None:
        await self._call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret,
                "allowed_updates": ["message"],
                "drop_pending_updates": False,
            },
        )

    async def delete_webhook(self) -> None:
        await self._call("deleteWebhook", {"drop_pending_updates": False})
