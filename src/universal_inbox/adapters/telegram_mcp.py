"""Dependency-free Telegram MCP read adapter seam."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts import InboxCursor, InboxItem, ItemIdentity
from ._read_only import ReadOnlyInboxAdapter


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
        cursor=InboxCursor(record.cursor, source=source) if record.cursor is not None else None,
    )


class TelegramMcpReadAdapter(ReadOnlyInboxAdapter[TelegramMcpPreview]):
    """Normalize injected MCP `read` results without owning credentials or transport."""

    def __init__(self, *, adapter_id: str, reader, capabilities=()) -> None:
        super().__init__(
            adapter_id=adapter_id,
            source="telegram",
            reader=reader,
            item_mapper=_telegram_item_mapper,
            capabilities=capabilities,
        )
