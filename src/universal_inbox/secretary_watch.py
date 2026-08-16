"""Opt-in read-only watcher that forwards canonical item refs to a wake sink."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from .adapter import InboxAdapter
from .contracts import InboxCursor, ItemIdentity
from .store import SQLiteInboxStore


WakeSink = Callable[[str], None]


class WakeClaimUnavailable(RuntimeError):
    """Another live watcher owns the wake lease; retry without advancing."""


@dataclass(frozen=True, slots=True)
class WatchResult:
    item_count: int
    emitted_refs: tuple[str, ...]
    next_cursor: InboxCursor | None


class SecretaryWatch:
    """Poll one injected adapter and emit bounded opaque refs after ingestion.

    The cursor is advanced only after every newly ingested item is accepted by
    the sink.  If the sink raises, the cursor remains unchanged; a later poll
    sees the item again, while the durable item store suppresses a duplicate
    insertion and allows the wake to be retried.
    """

    def __init__(
        self,
        store: SQLiteInboxStore,
        adapter: InboxAdapter,
        wake_sink: WakeSink,
        *,
        max_ref_length: int = 256,
        wake_lease_seconds: float = 120.0,
    ) -> None:
        if max_ref_length < 32:
            raise ValueError("max_ref_length is too small")
        if wake_lease_seconds <= 0:
            raise ValueError("wake_lease_seconds must be positive")
        self._store = store
        self._adapter = adapter
        self._wake_sink = wake_sink
        self._max_ref_length = max_ref_length
        self._wake_lease_seconds = wake_lease_seconds
        self._owner = f"watch:{uuid4().hex}"
        self._poll_lock = threading.Lock()

    def poll_once(self, *, limit: int = 100) -> WatchResult:
        if not self._poll_lock.acquire(blocking=False):
            raise WakeClaimUnavailable("watch poll is already running")
        try:
            return self._poll_once(limit=limit)
        finally:
            self._poll_lock.release()

    def _poll_once(self, *, limit: int = 100) -> WatchResult:
        source = self._adapter.manifest.source
        cursor = self._store.get_source_cursor(source)
        batch = self._adapter.poll(cursor, limit=limit)
        emitted: list[str] = []
        for item in batch.items:
            self._store.ingest(item, advance_cursor=False)
            ref, delivered, claimed, claim_epoch = self._store.claim_wake(
                item.identity,
                self._ref(item.identity),
                self._owner,
                lease_seconds=self._wake_lease_seconds,
            )
            if delivered:
                continue
            if not claimed:
                raise WakeClaimUnavailable("wake is claimed by another dispatcher")
            try:
                self._wake_sink(ref)
            except Exception:
                self._store.release_wake_claim(item.identity, self._owner, claim_epoch)
                raise
            if not self._store.mark_wake_delivered(item.identity, self._owner, claim_epoch):
                raise WakeClaimUnavailable("wake claim was replaced before completion")
            emitted.append(ref)
        self._store.advance_source_cursor(source, batch.next_cursor)
        return WatchResult(len(batch.items), tuple(emitted), batch.next_cursor)

    def _ref(self, identity: ItemIdentity) -> str:
        ref = f"inbox://{identity.source}/{identity.item_id}"
        if len(ref) > self._max_ref_length:
            raise ValueError("item reference exceeds configured bound")
        return ref
