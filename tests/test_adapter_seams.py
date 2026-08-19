from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from universal_inbox.adapter import PermanentAdapterError
from universal_inbox.__main__ import _default_registry
from universal_inbox.adapters._read_only import ReadOnlyPage
from universal_inbox.adapters.gmail import GmailHimalayaReader, GmailPreview, GmailReadAdapter
from universal_inbox.adapters.telegram_mcp import TelegramMcpPreview, TelegramMcpReadAdapter
from universal_inbox.consumer import HermesNeutralConsumer
from universal_inbox.contracts import Capability, InboxCursor, InboxItem, ItemIdentity


def test_gmail_adapter_maps_injected_reader_to_canonical_poll_batch_and_status() -> None:
    calls: list[tuple[InboxCursor | None, int]] = []

    def reader(cursor: InboxCursor | None, limit: int) -> ReadOnlyPage[GmailPreview]:
        calls.append((cursor, limit))
        return ReadOnlyPage(
            items=(
                GmailPreview(message_id="gm-1", subject="Inbox orchid", snippet="first preview", sender="orchid@example.invalid", cursor="gmail-c-1"),
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
    assert batch.items[0].sender == "orchid@example.invalid"
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


def test_gmail_himalaya_reader_maps_envelopes_and_resumes_after_cursor() -> None:
    payload = {
        "envelopes": [
            {
                "id": "43297",
                "message-id": "new@example.invalid",
                "subject": "Newest",
                "from": [{"name": "Sender", "email": "sender@example.invalid"}],
                "date": "2026-08-09T10:00:00Z",
            },
            {
                "id": "43296",
                "message-id": "old@example.invalid",
                "subject": "Older",
                "from": [],
                "date": "2026-08-09T09:00:00Z",
            },
        ]
    }
    calls: list[tuple[str, ...]] = []

    message_payloads = {
        "43296": {"text_body": [0], "parts": [{"body": {"Text": "Older body"}}]},
        "43297": {"text_body": [0], "parts": [{"body": "Newest body"}]},
    }

    class Runner:
        def run(self, argv: tuple[str, ...], *, timeout_seconds: float, max_stdout_bytes: int) -> str:
            calls.append(argv)
            response = payload if argv[4:6] == ("envelope", "list") else message_payloads[argv[-1]]
            return __import__("json").dumps(response)

    reader = GmailHimalayaReader(Runner(), account="gmail", mailbox="Inbox")

    first = reader(None, 10)
    assert [item.message_id for item in first.items] == ["old@example.invalid", "new@example.invalid"]
    assert first.next_cursor == "new@example.invalid"
    assert first.items[0].subject == "Older"
    assert first.items[0].snippet == "Older body"
    assert first.items[1].sender == "Sender <sender@example.invalid>"
    assert first.items[1].snippet == "Newest body"

    resumed = reader(InboxCursor("old@example.invalid", source="gmail"), 10)
    assert [item.message_id for item in resumed.items] == ["new@example.invalid"]
    assert resumed.next_cursor == "new@example.invalid"
    assert calls[0][:6] == ("himalaya", "-a", "gmail", "--json", "envelope", "list")
    assert ("himalaya", "-a", "gmail", "--backend", "imap", "--json", "message", "read", "-m", "Inbox", "43296") in calls


def test_gmail_himalaya_reader_preserves_html_body_for_sandboxed_display() -> None:
    assert GmailHimalayaReader._plain_text_body(
        {"text_body": [], "html_body": [0], "parts": [{"body": "<p>Hello <b>world</b></p>"}]}
    ) == "<p>Hello <b>world</b></p>"


def test_default_registry_registers_both_allowlisted_gmail_accounts() -> None:
    manifests = _default_registry().manifests()
    assert [manifest.adapter_id for manifest in manifests] == [
        "gmail-gmail-inbox",
        "gmail-careviolan-inbox",
    ]
    assert [manifest.source for manifest in manifests] == ["gmail", "gmail:careviolan"]


def test_telegram_adapter_maps_injected_reader_to_canonical_poll_batch_and_status() -> None:
    calls: list[tuple[InboxCursor | None, int]] = []

    def reader(cursor: InboxCursor | None, limit: int) -> ReadOnlyPage[TelegramMcpPreview]:
        calls.append((cursor, limit))
        return ReadOnlyPage(
            items=(
                TelegramMcpPreview(
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

    adapter = TelegramMcpReadAdapter(
        adapter_id="telegram-mcp-adapter",
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
    assert status.adapter_id == "telegram-mcp-adapter"
    assert status.cursor == InboxCursor("telegram-c-8", source="telegram")
    assert status.item_count == 1
    assert adapter.get(ItemIdentity("telegram", "chat-42:m-7")) == batch.items[0]


def test_telegram_adapter_filters_to_explicit_chat_allowlist() -> None:
    def reader(_cursor: InboxCursor | None, _limit: int) -> ReadOnlyPage[TelegramMcpPreview]:
        return ReadOnlyPage(
            items=(
                TelegramMcpPreview(chat_id="dm-1", message_id="m-1", text="allowed dm"),
                TelegramMcpPreview(chat_id="group-1", message_id="m-2", text="allowed group"),
                TelegramMcpPreview(chat_id="unselected", message_id="m-3", text="must not wake"),
            ),
            next_cursor="telegram-c-3",
            capabilities=frozenset({Capability.POLL}),
        )

    adapter = TelegramMcpReadAdapter(
        adapter_id="telegram-allowlisted",
        reader=reader,
        allowed_chat_ids={"dm-1", "group-1"},
    )

    batch = adapter.poll(None, limit=10)

    assert [item.identity for item in batch.items] == [
        ItemIdentity("telegram", "dm-1:m-1"),
        ItemIdentity("telegram", "group-1:m-2"),
    ]


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
