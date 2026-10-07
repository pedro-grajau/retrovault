"""Telegram update validation and Bot API delivery."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx

from app.modules.concierge.domain.session import IncomingMessage

_START_COMMAND = re.compile(r"^/start(?:\s+([A-Za-z0-9_-]{1,64}))?\s*$", re.IGNORECASE)
_POSTGRES_BIGINT_MAX = 2**63 - 1


class InvalidTelegramUpdate(ValueError):
    """The Telegram update is malformed or cannot be processed safely."""


class TelegramUnavailable(RuntimeError):
    """Telegram could not accept an outbound API call."""


class TelegramUpdateAdapter:
    def __init__(self, allowed_user_ids: frozenset[int]) -> None:
        self.allowed_user_ids = allowed_user_ids

    def normalize(
        self,
        payload: object,
        *,
        correlation_id: UUID,
        now: datetime | None = None,
    ) -> IncomingMessage | None:
        if not isinstance(payload, dict):
            raise InvalidTelegramUpdate("invalid_update")
        update_id = payload.get("update_id")
        if type(update_id) is not int or not 0 <= update_id <= _POSTGRES_BIGINT_MAX:
            raise InvalidTelegramUpdate("invalid_update_id")
        message = payload.get("message")
        if message is None:
            return None
        if not isinstance(message, dict):
            raise InvalidTelegramUpdate("invalid_message")
        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            raise InvalidTelegramUpdate("invalid_sender")
        if chat.get("type") != "private":
            return None
        chat_id = chat.get("id")
        user_id = sender.get("id")
        message_id = message.get("message_id")
        sent_epoch = message.get("date")
        if (
            type(chat_id) is not int
            or chat_id <= 0
            or type(user_id) is not int
            or user_id <= 0
            or type(message_id) is not int
            or not 0 < message_id <= _POSTGRES_BIGINT_MAX
            or type(sent_epoch) is not int
            or sent_epoch <= 0
        ):
            raise InvalidTelegramUpdate("invalid_message_metadata")
        if sender.get("is_bot") is True or user_id not in self.allowed_user_ids:
            return None
        if chat_id != user_id:
            return None
        current = now or datetime.now(UTC)
        if sent_epoch > int(current.timestamp()) + 60:
            raise InvalidTelegramUpdate("future_message")
        try:
            sent_at = datetime.fromtimestamp(sent_epoch, UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise InvalidTelegramUpdate("invalid_message_date") from exc
        text = message.get("text")
        if not isinstance(text, str) or not text or len(text) > 4096:
            return None
        if any(
            ord(character) < 32 and character not in "\t\r\n"
            for character in text
        ):
            return None
        start_match = _START_COMMAND.fullmatch(text)
        reference = start_match.group(1) if start_match else None
        return IncomingMessage(
            channel="telegram",
            external_user_id=str(user_id),
            external_chat_id=str(chat_id),
            update_id=update_id,
            message_id=message_id,
            text=text,
            sent_at=sent_at,
            received_at=current,
            correlation_id=correlation_id,
            context_reference=reference,
        )


class TelegramBotClient:
    def __init__(
        self,
        bot_token: str,
        *,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def send_chat_action(self, chat_id: str, action: str = "typing") -> None:
        if action != "typing":
            raise ValueError("unsupported_chat_action")
        await self._call("sendChatAction", {"chat_id": chat_id, "action": action})

    async def send_message(self, chat_id: str, text: str) -> None:
        await self._call("sendMessage", {"chat_id": chat_id, "text": text})

    async def _call(self, method: str, payload: dict[str, str]) -> None:
        if not self._bot_token:
            raise TelegramUnavailable("telegram_bot_not_configured")
        url = f"https://api.telegram.org/bot{self._bot_token}/{method}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds, transport=self._transport
            ) as client:
                response = await client.post(url, json=payload)
            response.raise_for_status()
            body: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramUnavailable("telegram_delivery_failed") from exc
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise TelegramUnavailable("telegram_delivery_failed")
