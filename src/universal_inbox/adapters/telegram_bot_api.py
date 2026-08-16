"""Read-only Telegram Bot API ingress with a durable update cursor."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from typing import Any

from ..adapter import MalformedAdapterResponse, TransientAdapterError
from ..contracts import Capability, InboxCursor
from ._read_only import ReadOnlyPage
from .telegram_mcp import TelegramMcpPreview


class TelegramBotApiReader:
    """Read allowlisted text messages without owning routing or delivery policy."""

    def __init__(
        self,
        token: str,
        *,
        allowed_chat_ids: Iterable[str],
        own_bot_id: str,
        ignored_sender_ids: Iterable[str] = (),
        timeout_seconds: float = 10,
        runner: Any = urllib.request.urlopen,
    ) -> None:
        self._token = token.strip()
        self._allowed_chat_ids = frozenset(str(value).strip() for value in allowed_chat_ids if str(value).strip())
        self._own_bot_id = str(own_bot_id).strip()
        self._ignored_sender_ids = frozenset(str(value).strip() for value in ignored_sender_ids if str(value).strip())
        self._timeout_seconds = timeout_seconds
        self._runner = runner
        if not self._token or not self._allowed_chat_ids or not self._own_bot_id:
            raise ValueError("Telegram Bot API reader requires token, bot id, and chat allowlist")
        if timeout_seconds <= 0:
            raise ValueError("Telegram Bot API timeout must be positive")

    def __call__(self, cursor: InboxCursor | None, limit: int) -> ReadOnlyPage[TelegramMcpPreview]:
        if not 1 <= limit <= 100:
            raise ValueError("Telegram Bot API limit must be between 1 and 100")
        if cursor is not None and cursor.source not in {None, "telegram"}:
            raise ValueError("Telegram cursor source mismatch")
        offset = self._decode_cursor(cursor)
        payload = {
            "allowed_updates": json.dumps(["message", "channel_post"], separators=(",", ":")),
            "limit": "100",
            "timeout": "0",
        }
        if offset is not None:
            payload["offset"] = str(offset)
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/getUpdates",
            data=urllib.parse.urlencode(payload).encode(),
            method="POST",
        )
        try:
            with self._runner(request, timeout=self._timeout_seconds) as response:
                if not 200 <= int(response.status) < 300:
                    raise RuntimeError(f"Telegram getUpdates returned HTTP {response.status}")
                result = json.loads(response.read())
        except json.JSONDecodeError as error:
            raise MalformedAdapterResponse("Telegram getUpdates returned invalid JSON") from error
        if not isinstance(result, Mapping) or result.get("ok") is not True or not isinstance(result.get("result"), list):
            raise MalformedAdapterResponse("Telegram getUpdates returned an invalid response")
        updates = result["result"]
        if any(not isinstance(update, Mapping) for update in updates):
            raise MalformedAdapterResponse("Telegram getUpdates returned a malformed update")
        update_ids = [self._update_id(update) for update in updates]
        next_cursor = str(max(update_ids) + 1) if update_ids else str(offset) if offset is not None else "0"
        if cursor is None:
            return ReadOnlyPage((), next_cursor, frozenset({Capability.POLL}))
        items: list[TelegramMcpPreview] = []
        for update in updates:
            preview = self._preview(update, next_cursor)
            if preview is not None:
                items.append(preview)
        if len(items) > limit:
            raise TransientAdapterError("Telegram getUpdates returned more messages than the inbox limit")
        return ReadOnlyPage(tuple(items), next_cursor, frozenset({Capability.POLL}))

    @staticmethod
    def _decode_cursor(cursor: InboxCursor | None) -> int | None:
        if cursor is None:
            return None
        try:
            offset = int(cursor.value)
        except ValueError as error:
            raise MalformedAdapterResponse("Telegram Bot API cursor must be an integer") from error
        if offset < 0:
            raise MalformedAdapterResponse("Telegram Bot API cursor must not be negative")
        return offset

    @staticmethod
    def _update_id(update: Mapping[str, object]) -> int:
        value = update.get("update_id")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise MalformedAdapterResponse("Telegram update has no valid update id")
        return value

    def _preview(self, update: Mapping[str, object], cursor: str) -> TelegramMcpPreview | None:
        message = update.get("message") or update.get("channel_post")
        if not isinstance(message, Mapping):
            return None
        chat = message.get("chat")
        if not isinstance(chat, Mapping):
            raise MalformedAdapterResponse("Telegram message has no chat")
        chat_id = str(chat.get("id") or "").strip()
        if chat_id not in self._allowed_chat_ids:
            return None
        message_id = message.get("message_id")
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            raise MalformedAdapterResponse("Telegram message has no valid message id")
        text = message.get("text")
        if not isinstance(text, str):
            return None
        sender_info = message.get("from") or message.get("sender_chat")
        sender = str(sender_info.get("id") or "").strip() if isinstance(sender_info, Mapping) else ""
        if not sender:
            raise MalformedAdapterResponse("Telegram text message has no sender")
        if sender == self._own_bot_id or sender in self._ignored_sender_ids:
            return None
        return TelegramMcpPreview(chat_id, str(message_id), text, sender=sender, cursor=cursor)
