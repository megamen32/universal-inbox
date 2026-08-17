"""Dependency-free Telegram MCP read adapter seam."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts import InboxCursor, InboxItem, ItemIdentity
from ._read_only import ReadOnlyInboxAdapter, ReadOnlyPage


@dataclass(frozen=True, slots=True)
class TelegramMcpPreview:
    chat_id: str
    message_id: str
    text: str | None = None
    sender: str | None = None
    cursor: str | None = None


def _telegram_item_mapper(record: TelegramMcpPreview, source: str) -> InboxItem:
    identity = ItemIdentity(source=source, item_id=f"{record.chat_id}:{record.message_id}")
    return InboxItem(
        identity=identity,
        title=record.sender or record.chat_id,
        body=record.text,
        sender=record.sender or record.chat_id,
        cursor=InboxCursor(record.cursor, source=source) if record.cursor is not None else None,
    )


class TelegramMcpReadAdapter(ReadOnlyInboxAdapter[TelegramMcpPreview]):
    """Normalize injected MCP `read` results without owning credentials or transport."""

    def __init__(self, *, adapter_id: str, reader, capabilities=(), allowed_chat_ids=None) -> None:
        normalized_allowlist = None if allowed_chat_ids is None else frozenset(
            chat_id.strip() for chat_id in allowed_chat_ids if isinstance(chat_id, str) and chat_id.strip()
        )
        if allowed_chat_ids is not None and not normalized_allowlist:
            raise ValueError("allowed_chat_ids must contain at least one chat id")

        def filtered_reader(cursor, limit):
            page = reader(cursor, limit)
            if normalized_allowlist is None:
                return page
            return ReadOnlyPage(
                items=tuple(item for item in page.items if item.chat_id in normalized_allowlist),
                next_cursor=page.next_cursor,
                capabilities=page.capabilities,
            )

        super().__init__(
            adapter_id=adapter_id,
            source="telegram",
            reader=filtered_reader,
            item_mapper=_telegram_item_mapper,
            capabilities=capabilities,
        )
