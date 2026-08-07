from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from universal_inbox.adapter import PermanentAdapterError
from universal_inbox.adapters._read_only import ReadOnlyPage
from universal_inbox.adapters.gmail import GmailPreview, GmailReadAdapter
from universal_inbox.adapters.telegram_web import TelegramWebPreview, TelegramWebReadAdapter
from universal_inbox.consumer import HermesNeutralConsumer
from universal_inbox.contracts import Capability, InboxCursor, InboxItem, ItemIdentity


def test_gmail_adapter_maps_injected_reader_to_canonical_poll_batch_and_status() -> None:
    calls: list[tuple[InboxCursor | None, int]] = []

    def reader(cursor: InboxCursor | None, limit: int) -> ReadOnlyPage[GmailPreview]:
        calls.append((cursor, limit))
        return ReadOnlyPage(
            items=(
                GmailPreview(message_id="gm-1", subject="Inbox orchid", snippet="first preview", cursor="gmail-c-1"),
                GmailPreview(message_id="gm-2", subject="Inbox lily", snippet="second preview", cursor="gmail-c-1"),
            ),
            next_cursor="gmail-c-2",
            capabilities=frozenset({Capability.POLL, Capability.SEARCH}),
        )

    adapter = GmailReadAdapter(
        adapter_id="gmail-adapter",
        reader=reader,
        capabilities=frozenset({Capability.POLL, Capability.SEARCH}),
    )

    batch = adapter.poll(None, limit=5)

    assert calls == [(None, 5)]
    assert batch.capabilities == frozenset({Capability.POLL, Capability.SEARCH})
    assert batch.next_cursor == InboxCursor("gmail-c-2", source="gmail")
    assert [item.identity for item in batch.items] == [
        ItemIdentity("gmail", "gm-1"),
        ItemIdentity("gmail", "gm-2"),
    ]
    assert batch.items[0].title == "Inbox orchid"
    assert batch.items[0].body == "first preview"
    assert batch.items[0].cursor == InboxCursor("gmail-c-1", source="gmail")

    status = adapter.status()
    assert status.source == "gmail"
    assert status.adapter_id == "gmail-adapter"
    assert status.cursor == InboxCursor("gmail-c-2", source="gmail")
    assert status.item_count == 2
    assert status.accepted_item_ids == (
        ItemIdentity("gmail", "gm-1"),
        ItemIdentity("gmail", "gm-2"),
    )
    assert adapter.get(ItemIdentity("gmail", "gm-1")) == batch.items[0]
    with pytest.raises(PermanentAdapterError):
        adapter.execute(object())  # type: ignore[arg-type]


def test_telegram_adapter_maps_injected_reader_to_canonical_poll_batch_and_status() -> None:
    calls: list[tuple[InboxCursor | None, int]] = []

    def reader(cursor: InboxCursor | None, limit: int) -> ReadOnlyPage[TelegramWebPreview]:
        calls.append((cursor, limit))
        return ReadOnlyPage(
            items=(
                TelegramWebPreview(
                    chat_id="chat-42",
                    message_id="m-7",
                    sender="Ari",
                    text="Telegram preview",
                    cursor="telegram-c-7",
                ),
            ),
            next_cursor="telegram-c-8",
            capabilities=frozenset({Capability.POLL}),
        )

    adapter = TelegramWebReadAdapter(
        adapter_id="telegram-web-adapter",
        reader=reader,
        capabilities=frozenset({Capability.POLL}),
    )

    batch = adapter.poll(None, limit=3)

    assert calls == [(None, 3)]
    assert batch.capabilities == frozenset({Capability.POLL})
    assert batch.next_cursor == InboxCursor("telegram-c-8", source="telegram")
    assert batch.items[0].identity == ItemIdentity("telegram", "chat-42:m-7")
    assert batch.items[0].title == "Ari"
    assert batch.items[0].body == "Telegram preview"
    assert batch.items[0].cursor == InboxCursor("telegram-c-7", source="telegram")

    status = adapter.status()
    assert status.source == "telegram"
    assert status.adapter_id == "telegram-web-adapter"
    assert status.cursor == InboxCursor("telegram-c-8", source="telegram")
    assert status.item_count == 1
    assert adapter.get(ItemIdentity("telegram", "chat-42:m-7")) == batch.items[0]


def test_consumer_calls_core_surface_search_and_digest_only() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeSurface:
        def dispatch(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
            calls.append((name, arguments))
            return {"ok": True, "name": name, "arguments": arguments}

    consumer = HermesNeutralConsumer(FakeSurface())

    search = consumer.search("  orchid  ", limit=250)
    digest = consumer.digest_candidates(limit=0)

    assert calls == [
        ("inbox.search", {"query": "orchid", "limit": 100}),
        ("inbox.digest_candidates", {"limit": 1}),
    ]
    assert search["ok"] is True
    assert digest["name"] == "inbox.digest_candidates"
    assert not hasattr(consumer, "send")
    with pytest.raises(ValueError):
        consumer.search("   ")
