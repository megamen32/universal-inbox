"""Dependency-free read-only adapter helpers.

These seams intentionally accept injected callables and never own credentials,
browser state, or send/download actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from ..adapter import AdapterManifest, PermanentAdapterError
from ..contracts import Capability, InboxCursor, InboxItem, ItemIdentity, PollBatch, SourceStatus


@dataclass(frozen=True, slots=True)
class ReadOnlyInboxRecord:
    item_id: str
    title: str | None = None
    body: str | None = None
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class ReadOnlyPollPage:
    items: tuple[ReadOnlyInboxRecord, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class ReadOnlyAdapterStatus:
    cursor: str | None = None
    item_count: int = 0
    last_attempted_at: str | None = None
    last_success_at: str | None = None


class ReadOnlyPollReader(Protocol):
    def __call__(self, cursor: InboxCursor | None, limit: int) -> ReadOnlyPollPage: ...


class ReadOnlyStatusReader(Protocol):
    def __call__(self) -> ReadOnlyAdapterStatus: ...


class ReadOnlyLookupReader(Protocol):
    def __call__(self, identity: ItemIdentity) -> ReadOnlyInboxRecord | None: ...


def _normalize_source(value: str) -> str:
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError("source must not be empty")
    return normalized


def _normalize_cursor(source: str, value: str | None) -> InboxCursor | None:
    if value is None:
        return None
    return InboxCursor(value=value, source=source)


def _record_to_item(source: str, record: ReadOnlyInboxRecord) -> InboxItem:
    identity = ItemIdentity(source=source, item_id=record.item_id)
    return InboxItem(
        identity=identity,
        title=record.title,
        body=record.body,
        cursor=_normalize_cursor(source, record.cursor),
    )


class ReadOnlyInboxAdapter:
    """Base implementation for read-only adapter seams."""

    def __init__(
        self,
        *,
        adapter_id: str,
        source: str,
        poll_reader: ReadOnlyPollReader,
        status_reader: ReadOnlyStatusReader | None = None,
        lookup_reader: ReadOnlyLookupReader | None = None,
        capabilities: frozenset[Capability] | None = None,
    ) -> None:
        self.manifest = AdapterManifest(
            adapter_id=adapter_id,
            source=_normalize_source(source),
            capabilities=capabilities or frozenset({Capability.POLL, Capability.SEARCH}),
        )
        self._poll_reader = poll_reader
        self._status_reader = status_reader
        self._lookup_reader = lookup_reader
        self._last_cursor: InboxCursor | None = None
        self._last_item_count = 0
        self._last_attempted_at: str | None = None
        self._last_success_at: str | None = None

    def status(self) -> SourceStatus:
        snapshot = self._status_reader() if self._status_reader is not None else ReadOnlyAdapterStatus(
            cursor=self._last_cursor.value if self._last_cursor is not None else None,
            item_count=self._last_item_count,
            last_attempted_at=self._last_attempted_at,
            last_success_at=self._last_success_at,
        )
        cursor = _normalize_cursor(self.manifest.source, snapshot.cursor)
        return SourceStatus(
            source=self.manifest.source,
            adapter_id=self.manifest.adapter_id,
            cursor=cursor,
            item_count=snapshot.item_count,
            last_attempted_at=snapshot.last_attempted_at,
            last_success_at=snapshot.last_success_at,
        )

    def poll(self, cursor: InboxCursor | None, *, limit: int) -> PollBatch:
        if limit < 1:
            raise ValueError("limit must be positive")
        page = self._poll_reader(cursor, limit)
        items = tuple(_record_to_item(self.manifest.source, record) for record in page.items)
        next_cursor = _normalize_cursor(self.manifest.source, page.next_cursor)
        self._last_cursor = next_cursor
        self._last_item_count = len(items)
        self._last_attempted_at = None
        self._last_success_at = None
        return PollBatch(items=items, next_cursor=next_cursor, capabilities=self.manifest.capabilities)

    def get(self, identity: ItemIdentity) -> InboxItem | None:
        if identity.source != self.manifest.source:
            return None
        if self._lookup_reader is None:
            return None
        record = self._lookup_reader(identity)
        return None if record is None else _record_to_item(self.manifest.source, record)

    def execute(self, action):  # type: ignore[no-untyped-def]
        raise PermanentAdapterError("read-only adapter does not support outbound actions")
