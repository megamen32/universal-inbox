"""Sequential poll coordinator for Universal Inbox adapters."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from .adapter import (
    InboxAdapter,
    MalformedAdapterResponse,
    PermanentAdapterError,
    TransientAdapterError,
)
from .contracts import Capability, InboxCursor, InboxItem, ItemIdentity, PollReceipt, SourceStatus
from .registry import AdapterRegistry
from .store import SQLiteInboxStore


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class PollFailure:
    adapter_id: str
    source: str
    request_id: str
    error_class: str
    retry_after_seconds: int | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class PollAttemptResult:
    adapter_id: str
    source: str
    request_id: str
    accepted_item_ids: tuple[ItemIdentity, ...]
    inserted_item_count: int
    item_count: int
    accepted_cursor: InboxCursor | None
    receipt_recorded: bool
    failure: PollFailure | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


@dataclass(frozen=True, slots=True)
class PollRunResult:
    attempts: tuple[PollAttemptResult, ...]
    partial_failures: tuple[PollFailure, ...]


class PollingCoordinator:
    def __init__(self, store: SQLiteInboxStore, registry: AdapterRegistry) -> None:
        self._store = store
        self._registry = registry
        self._locks: dict[str, threading.Lock] = {}
        self._lock_guard = threading.Lock()

    def poll_once(
        self,
        adapter_id: str,
        *,
        limit: int = 100,
        request_id: str | None = None,
    ) -> PollAttemptResult:
        adapter = self._registry.get(adapter_id)
        manifest = adapter.manifest
        source = manifest.source
        lock = self._lock_for(source)
        request_id = request_id or uuid4().hex
        with lock:
            baseline = self._current_status(source, manifest.adapter_id)
            attempted_at = _utc_now()
            try:
                batch = adapter.poll(baseline.cursor, limit=limit)
                self._validate_batch(source, batch, limit)
                accepted_item_ids = tuple(item.identity for item in batch.items)
                inserted_item_count = 0
                for item in batch.items:
                    if self._store.ingest(item):
                        inserted_item_count += 1
                if batch.next_cursor is not None:
                    self._store.advance_source_cursor(source, batch.next_cursor)
                receipt = PollReceipt(
                    source=source,
                    adapter_id=manifest.adapter_id,
                    request_id=request_id,
                    accepted_item_ids=accepted_item_ids,
                    accepted_cursor=batch.next_cursor,
                    item_count=len(batch.items),
                )
                receipt_recorded = self._store.record_poll_receipt(receipt)
                self._store.record_source_status(
                    SourceStatus(
                        source=source,
                        adapter_id=manifest.adapter_id,
                        cursor=batch.next_cursor if batch.next_cursor is not None else baseline.cursor,
                        item_count=len(batch.items),
                        last_attempted_at=attempted_at,
                        last_success_at=attempted_at,
                        last_request_id=request_id,
                        last_receipt_request_id=request_id,
                        accepted_item_ids=accepted_item_ids,
                    )
                )
                return PollAttemptResult(
                    adapter_id=manifest.adapter_id,
                    source=source,
                    request_id=request_id,
                    accepted_item_ids=accepted_item_ids,
                    inserted_item_count=inserted_item_count,
                    item_count=len(batch.items),
                    accepted_cursor=batch.next_cursor,
                    receipt_recorded=receipt_recorded,
                )
            except (TransientAdapterError, PermanentAdapterError, MalformedAdapterResponse) as exc:
                failure_class = type(exc).__name__
                retry_after = exc.retry_after_seconds
                failure = PollFailure(
                    adapter_id=manifest.adapter_id,
                    source=source,
                    request_id=request_id,
                    error_class=failure_class,
                    retry_after_seconds=retry_after,
                    message=str(exc),
                )
                self._store.record_poll_receipt(
                    PollReceipt(
                        source=source,
                        adapter_id=manifest.adapter_id,
                        request_id=request_id,
                        accepted_item_ids=(),
                        accepted_cursor=None,
                        item_count=0,
                        error_class=failure_class,
                        retry_after_seconds=retry_after,
                    )
                )
                self._store.record_source_status(
                    SourceStatus(
                        source=source,
                        adapter_id=manifest.adapter_id,
                        cursor=baseline.cursor,
                        item_count=baseline.item_count,
                        last_attempted_at=attempted_at,
                        last_success_at=baseline.last_success_at,
                        last_request_id=request_id,
                        last_receipt_request_id=baseline.last_receipt_request_id,
                        error_class=failure_class,
                        retry_after_seconds=retry_after,
                        accepted_item_ids=baseline.accepted_item_ids,
                    )
                )
                return PollAttemptResult(
                    adapter_id=manifest.adapter_id,
                    source=source,
                    request_id=request_id,
                    accepted_item_ids=(),
                    inserted_item_count=0,
                    item_count=0,
                    accepted_cursor=None,
                    receipt_recorded=True,
                    failure=failure,
                )

    def poll_all(
        self,
        *,
        limit: int = 100,
        request_ids: dict[str, str] | None = None,
    ) -> PollRunResult:
        attempts: list[PollAttemptResult] = []
        failures: list[PollFailure] = []
        request_ids = request_ids or {}
        for adapter in self._registry.adapters():
            result = self.poll_once(
                adapter.manifest.adapter_id,
                limit=limit,
                request_id=request_ids.get(adapter.manifest.adapter_id),
            )
            attempts.append(result)
            if result.failure is not None:
                failures.append(result.failure)
        return PollRunResult(attempts=tuple(attempts), partial_failures=tuple(failures))

    def _current_status(self, source: str, adapter_id: str) -> SourceStatus:
        status = self._store.get_source_status(source)
        if status is not None:
            if status.adapter_id != adapter_id:
                return SourceStatus(
                    source=source,
                    adapter_id=adapter_id,
                    cursor=status.cursor,
                    item_count=status.item_count,
                    last_attempted_at=status.last_attempted_at,
                    last_success_at=status.last_success_at,
                    last_request_id=status.last_request_id,
                    last_receipt_request_id=status.last_receipt_request_id,
                    error_class=status.error_class,
                    retry_after_seconds=status.retry_after_seconds,
                    accepted_item_ids=status.accepted_item_ids,
                )
            return status
        return SourceStatus(
            source=source,
            adapter_id=adapter_id,
            cursor=self._store.get_source_cursor(source),
        )

    @staticmethod
    def _validate_batch(source: str, batch: object, limit: int) -> None:
        if not hasattr(batch, "items") or not hasattr(batch, "next_cursor") or not hasattr(batch, "capabilities"):
            raise MalformedAdapterResponse("poll must return a PollBatch-like object")
        if limit < 1:
            raise MalformedAdapterResponse("limit must be positive")
        if len(getattr(batch, "items")) > limit:
            raise MalformedAdapterResponse("batch exceeds limit")
        capabilities = getattr(batch, "capabilities")
        if Capability.POLL not in capabilities:
            raise MalformedAdapterResponse("adapter must advertise POLL capability")
        items = getattr(batch, "items")
        next_cursor = getattr(batch, "next_cursor")
        if next_cursor is not None and next_cursor.source is not None and next_cursor.source != source:
            raise MalformedAdapterResponse("next cursor source must match adapter source")
        for item in items:
            if not isinstance(item, InboxItem):
                raise MalformedAdapterResponse("items must contain InboxItem instances")
            if item.identity.source != source:
                raise MalformedAdapterResponse("item source must match adapter source")
            if item.cursor is not None and item.cursor.source is not None and item.cursor.source != source:
                raise MalformedAdapterResponse("item cursor source must match adapter source")
            for ref in item.refs:
                if ref.identity.source != source:
                    raise MalformedAdapterResponse("item refs must match adapter source")

    def _lock_for(self, source: str) -> threading.Lock:
        normalized = source.strip().lower()
        with self._lock_guard:
            lock = self._locks.get(normalized)
            if lock is None:
                lock = threading.Lock()
                self._locks[normalized] = lock
            return lock
