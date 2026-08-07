"""Dependency-free Gmail read adapter seam."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts import InboxCursor, InboxItem, ItemIdentity
from ._read_only import ReadOnlyInboxAdapter


@dataclass(frozen=True, slots=True)
class GmailPreview:
    message_id: str
    subject: str | None = None
    snippet: str | None = None
    cursor: str | None = None


def _gmail_item_mapper(record: GmailPreview, source: str) -> InboxItem:
    identity = ItemIdentity(source=source, item_id=record.message_id)
    return InboxItem(
        identity=identity,
        title=record.subject,
        body=record.snippet,
        cursor=InboxCursor(record.cursor, source=source) if record.cursor is not None else None,
    )


class GmailReadAdapter(ReadOnlyInboxAdapter[GmailPreview]):
    def __init__(
        self,
        *,
        adapter_id: str,
        reader,
        capabilities=(),
    ) -> None:
        super().__init__(
            adapter_id=adapter_id,
            source="gmail",
            reader=reader,
            item_mapper=_gmail_item_mapper,
            capabilities=capabilities,
        )
