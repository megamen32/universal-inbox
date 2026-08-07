from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from universal_inbox.adapter import AdapterManifest, TransientAdapterError
from universal_inbox.contracts import Capability, InboxCursor, InboxItem, ItemIdentity, PollBatch, SourceStatus
from universal_inbox.polling import PollingCoordinator
from universal_inbox.registry import AdapterRegistry
from universal_inbox.store import SQLiteInboxStore


def _item(source: str, item_id: str, cursor: str, title: str) -> InboxItem:
    identity = ItemIdentity(source=source, item_id=item_id)
    return InboxItem(
        identity=identity,
        title=title,
        body=f"{title} body",
        cursor=InboxCursor(cursor, source=source),
    )


class ScriptedAdapter:
    def __init__(self, *, adapter_id: str, source: str, steps: list[object]) -> None:
        self.manifest = AdapterManifest(
            adapter_id=adapter_id,
            source=source,
            capabilities=frozenset({Capability.POLL}),
        )
        self._steps = list(steps)
        self._index = 0
        self._cursor: InboxCursor | None = None

    def status(self) -> SourceStatus:
        return SourceStatus(
            source=self.manifest.source,
            adapter_id=self.manifest.adapter_id,
            cursor=self._cursor,
            item_count=self._index,
        )

    def poll(self, cursor: InboxCursor | None, *, limit: int) -> PollBatch:
        assert limit > 0
        if self._index >= len(self._steps):
            raise AssertionError("poll called more times than scripted")
        step = self._steps[self._index]
        self._index += 1
        if isinstance(step, Exception):
            raise step
        assert isinstance(step, PollBatch)
        self._cursor = step.next_cursor
        return step

    def get(self, identity: ItemIdentity) -> InboxItem | None:
        return None

    def execute(self, action):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_polling_preserves_dedupe_status_and_receipts_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "inbox.sqlite3"
    alpha = ScriptedAdapter(
        adapter_id="alpha-adapter",
        source="alpha",
        steps=[
            PollBatch(
                items=(
                    _item("alpha", "a-1", "alpha-c-1", "Alpha one"),
                    _item("alpha", "a-2", "alpha-c-1", "Alpha two"),
                ),
                next_cursor=InboxCursor("alpha-c-1", source="alpha"),
                capabilities=frozenset({Capability.POLL}),
            ),
            PollBatch(
                items=(
                    _item("alpha", "a-2", "alpha-c-2", "Alpha two"),
                    _item("alpha", "a-3", "alpha-c-2", "Alpha three"),
                ),
                next_cursor=InboxCursor("alpha-c-2", source="alpha"),
                capabilities=frozenset({Capability.POLL}),
            ),
        ],
    )
    beta = ScriptedAdapter(
        adapter_id="beta-adapter",
        source="beta",
        steps=[
            PollBatch(
                items=(
                    _item("beta", "b-1", "beta-c-1", "Beta one"),
                ),
                next_cursor=InboxCursor("beta-c-1", source="beta"),
                capabilities=frozenset({Capability.POLL}),
            ),
            TransientAdapterError("temporary backend timeout", retry_after_seconds=60),
        ],
    )

    with SQLiteInboxStore(path) as store:
        coordinator = PollingCoordinator(store, AdapterRegistry([alpha, beta]))

        first = coordinator.poll_all(
            limit=10,
            request_ids={"alpha-adapter": "req-alpha-1", "beta-adapter": "req-beta-1"},
        )
        second = coordinator.poll_all(
            limit=10,
            request_ids={"alpha-adapter": "req-alpha-2", "beta-adapter": "req-beta-2"},
        )

        assert first.partial_failures == ()
        assert len(second.partial_failures) == 1
        assert second.partial_failures[0].source == "beta"

        assert [row.identity.item_id for row in store.recent(limit=10)] == [
            "a-3",
            "b-1",
            "a-2",
            "a-1",
        ]
        assert store.counts() == {"items": 4, "receipts": 0, "sources": 2}
        alpha_status = store.get_source_status("alpha")
        assert alpha_status is not None
        assert alpha_status.source == "alpha"
        assert alpha_status.adapter_id == "alpha-adapter"
        assert alpha_status.cursor == InboxCursor("alpha-c-2", source="alpha")
        assert alpha_status.item_count == 2
        assert alpha_status.last_request_id == "req-alpha-2"
        assert alpha_status.last_receipt_request_id == "req-alpha-2"
        assert alpha_status.accepted_item_ids == (
            ItemIdentity("alpha", "a-2"),
            ItemIdentity("alpha", "a-3"),
        )
        beta_status = store.get_source_status("beta")
        assert beta_status is not None
        assert beta_status.source == "beta"
        assert beta_status.cursor == InboxCursor("beta-c-1", source="beta")
        assert beta_status.item_count == 1
        assert beta_status.error_class == "TransientAdapterError"
        assert beta_status.retry_after_seconds == 60

        receipts = store.list_poll_receipts()
        assert [receipt.request_id for receipt in receipts] == [
            "req-alpha-1",
            "req-beta-1",
            "req-alpha-2",
            "req-beta-2",
        ]
        assert receipts[-1].error_class == "TransientAdapterError"
        assert receipts[-1].accepted_item_ids == ()

    reopened = SQLiteInboxStore(path)
    try:
        assert reopened.get_source_cursor("alpha") == InboxCursor("alpha-c-2", source="alpha")
        assert reopened.get_source_cursor("beta") == InboxCursor("beta-c-1", source="beta")
        alpha_status = reopened.get_source_status("alpha")
        assert alpha_status is not None
        assert alpha_status.source == "alpha"
        assert alpha_status.adapter_id == "alpha-adapter"
        assert alpha_status.cursor == InboxCursor("alpha-c-2", source="alpha")
        assert alpha_status.item_count == 2
        assert alpha_status.last_request_id == "req-alpha-2"
        assert alpha_status.last_receipt_request_id == "req-alpha-2"
        assert alpha_status.accepted_item_ids == (
            ItemIdentity("alpha", "a-2"),
            ItemIdentity("alpha", "a-3"),
        )
        beta_status = reopened.get_source_status("beta")
        assert beta_status is not None
        assert beta_status.source == "beta"
        assert beta_status.adapter_id == "beta-adapter"
        assert beta_status.cursor == InboxCursor("beta-c-1", source="beta")
        assert beta_status.item_count == 1
        assert beta_status.last_request_id == "req-beta-2"
        assert beta_status.last_receipt_request_id == "req-beta-1"
        assert beta_status.error_class == "TransientAdapterError"
        assert beta_status.retry_after_seconds == 60
        assert beta_status.accepted_item_ids == (ItemIdentity("beta", "b-1"),)
        assert [receipt.request_id for receipt in reopened.list_poll_receipts()] == [
            "req-alpha-1",
            "req-beta-1",
            "req-alpha-2",
            "req-beta-2",
        ]
    finally:
        reopened.close()
