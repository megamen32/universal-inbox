"""Shared helpers for dependency-free read-only inbox adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Generic, Protocol, TypeVar

from ..adapter import (
    AdapterManifest,
    AdapterError,
    InboxAdapter,
    MalformedAdapterResponse,
    PermanentAdapterError,
    TransientAdapterError,
)
from ..contracts import Capability, InboxCursor, InboxItem, ItemIdentity, PollBatch, SourceStatus, build_poll_batch

TRecord = TypeVar("TRecord")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class ReadOnlyPage(Generic[TRecord]):
    items: tuple[TRecord, ...] = ()
    next_cursor: str | None = None
    capabilities: frozenset[Capability] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))


class ReadOnlyReader(Protocol[TRecord]):
    def __call__(self, cursor: InboxCursor | None, limit: int) -> ReadOnlyPage[TRecord]: ...


ItemMapper = Callable[[TRecord, str], InboxItem]


class ReadOnlyInboxAdapter(InboxAdapter, Generic[TRecord]):
    """Fail-closed read-only adapter with injected reader and canonical mapping."""

    def __init__(
        self,
        *,
        adapter_id: str,
        source: str,
        reader: ReadOnlyReader[TRecord],
        item_mapper: ItemMapper[TRecord],
        capabilities: frozenset[Capability] | set[Capability] | tuple[Capability, ...] = (Capability.POLL,),
    ) -> None:
        self.manifest = AdapterManifest(
            adapter_id=adapter_id,
            source=source,
            capabilities=frozenset(capabilities) | frozenset({Capability.POLL}),
        )
        self._reader = reader
        self._item_mapper = item_mapper
        self._cursor: InboxCursor | None = None
        self._item_count = 0
        self._last_attempted_at: str | None = None
        self._last_success_at: str | None = None
        self._last_error_class: str | None = None
        self._retry_after_seconds: int | None = None
        self._cached_items: dict[ItemIdentity, InboxItem] = {}

    def status(self) -> SourceStatus:
        return SourceStatus(
            source=self.manifest.source,
            adapter_id=self.manifest.adapter_id,
            cursor=self._cursor,
            item_count=self._item_count,
            last_attempted_at=self._last_attempted_at,
            last_success_at=self._last_success_at,
            error_class=self._last_error_class,
            retry_after_seconds=self._retry_after_seconds,
            accepted_item_ids=tuple(self._cached_items),
        )

    def poll(self, cursor: InboxCursor | None, *, limit: int) -> PollBatch:
        if limit < 1:
            raise MalformedAdapterResponse("limit must be positive")
        attempted_at = _utc_now()
        self._last_attempted_at = attempted_at
        try:
            page = self._reader(cursor, limit)
            if not hasattr(page, "items") or not hasattr(page, "next_cursor") or not hasattr(page, "capabilities"):
                raise MalformedAdapterResponse("reader must return a ReadOnlyPage-like object")
            items = tuple(page.items)
            if len(items) > limit:
                raise MalformedAdapterResponse("reader returned more items than requested")
            mapped_items = tuple(self._item_mapper(record, self.manifest.source) for record in items)
            next_cursor = self._build_cursor(page.next_cursor)
            batch_capabilities = frozenset(self.manifest.capabilities | getattr(page, "capabilities"))
        except (TransientAdapterError, PermanentAdapterError, MalformedAdapterResponse):
            self._capture_failure()
            raise
        except Exception as exc:  # pragma: no cover - defensive boundary
            self._capture_failure(exc)
            raise TransientAdapterError("read-only reader failed") from exc

        self._cursor = next_cursor
        self._item_count = len(mapped_items)
        self._last_success_at = attempted_at
        self._last_error_class = None
        self._retry_after_seconds = None
        self._cached_items = {item.identity: item for item in mapped_items}
        return build_poll_batch(mapped_items, next_cursor=next_cursor, capabilities=batch_capabilities)

    def get(self, identity: ItemIdentity) -> InboxItem | None:
        if identity.source != self.manifest.source:
            return None
        return self._cached_items.get(identity)

    def execute(self, action):  # type: ignore[no-untyped-def]
        raise PermanentAdapterError("read-only adapter does not support outbound actions")

    def _build_cursor(self, value: str | None) -> InboxCursor | None:
        if value is None:
            return None
        return InboxCursor(value, source=self.manifest.source)

    def _capture_failure(self, exc: AdapterError | None = None) -> None:
        self._last_error_class = None if exc is None else type(exc).__name__
        self._retry_after_seconds = None if exc is None else exc.retry_after_seconds
