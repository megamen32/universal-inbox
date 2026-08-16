from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from universal_inbox.adapters._read_only import ReadOnlyPage
from universal_inbox.adapters.telegram_mcp import TelegramMcpPreview
from universal_inbox.contracts import Capability, InboxCursor
from universal_inbox.secretary_watch import SecretaryWatch
from universal_inbox.store import SQLiteInboxStore
from universal_inbox.telegram_runtime import build_configured_telegram_watch, build_telegram_watch, run_telegram_watch


def test_watch_preserves_cursor_and_suppresses_replayed_items(tmp_path) -> None:
    pages = [
        ReadOnlyPage(
            items=(TelegramMcpPreview("chat", "1", "hello"),),
            next_cursor="c1",
            capabilities=frozenset({Capability.POLL}),
        ),
        ReadOnlyPage(items=(), next_cursor="c1", capabilities=frozenset({Capability.POLL})),
    ]
    seen: list[str] = []
    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        watch = build_telegram_watch(store, lambda _cursor, _limit: pages.pop(0), seen.append)
        first = watch.poll_once()
        second = watch.poll_once()
        assert first.emitted_refs == ("inbox://telegram/chat:1",)
        assert second.emitted_refs == ()
        assert store.get_source_cursor("telegram") == InboxCursor("c1", source="telegram")
    assert seen == ["inbox://telegram/chat:1"]


def test_watch_does_not_advance_cursor_when_sink_fails(tmp_path) -> None:
    page = ReadOnlyPage(
        items=(TelegramMcpPreview("chat", "1", "hello"),),
        next_cursor="c1",
        capabilities=frozenset({Capability.POLL}),
    )

    def reader(_cursor, _limit):
        return page

    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        watch = build_telegram_watch(store, reader, lambda _ref: (_ for _ in ()).throw(RuntimeError("wake down")))
        with pytest.raises(RuntimeError, match="wake down"):
            watch.poll_once()
        assert store.get_source_cursor("telegram") is None


def test_watch_retries_ingested_second_item_before_advancing_cursor(tmp_path) -> None:
    page = ReadOnlyPage(
        items=(
            TelegramMcpPreview("chat", "1", "one"),
            TelegramMcpPreview("chat", "2", "two"),
        ),
        next_cursor="c2",
        capabilities=frozenset({Capability.POLL}),
    )
    calls = 0
    seen: list[str] = []

    def sink(ref: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("wake down")
        seen.append(ref)

    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        watch = build_telegram_watch(store, lambda _cursor, _limit: page, sink)
        with pytest.raises(RuntimeError, match="wake down"):
            watch.poll_once()
        assert store.get_source_cursor("telegram") is None
        result = watch.poll_once()
        assert result.emitted_refs == ("inbox://telegram/chat:2",)
        assert store.get_source_cursor("telegram") == InboxCursor("c2", source="telegram")
    assert seen == ["inbox://telegram/chat:1", "inbox://telegram/chat:2"]


def test_watch_does_not_advance_item_cursor_before_all_wakes_are_delivered(tmp_path) -> None:
    page = ReadOnlyPage(
        items=(
            TelegramMcpPreview("chat", "1", "one", cursor="item-c1"),
            TelegramMcpPreview("chat", "2", "two", cursor="item-c2"),
        ),
        next_cursor="batch-c2",
        capabilities=frozenset({Capability.POLL}),
    )
    calls = 0

    def sink(_ref: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("wake down")

    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        watch = build_telegram_watch(store, lambda _cursor, _limit: page, sink)
        with pytest.raises(RuntimeError, match="wake down"):
            watch.poll_once()
        assert store.get_source_cursor("telegram") is None


def test_concurrent_watchers_have_one_wake_owner(tmp_path) -> None:
    page = ReadOnlyPage(
        items=(TelegramMcpPreview("chat", "1", "hello", cursor="item-c1"),),
        next_cursor="batch-c1",
        capabilities=frozenset({Capability.POLL}),
    )
    started = threading.Event()
    release = threading.Event()
    sink_calls: list[str] = []
    second_errors: list[Exception] = []

    def first_sink(ref: str) -> None:
        sink_calls.append(ref)
        started.set()
        assert release.wait(2)

    first_cursor: InboxCursor | None = None

    def run_first() -> None:
        nonlocal first_cursor
        with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as first_store:
            first = build_telegram_watch(first_store, lambda _cursor, _limit: page, first_sink)
            first.poll_once()
            first_cursor = first_store.get_source_cursor("telegram")

    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as second_store:
        second = build_telegram_watch(second_store, lambda _cursor, _limit: page, sink_calls.append)

        first_thread = threading.Thread(target=run_first)
        first_thread.start()
        assert started.wait(2)
        try:
            second.poll_once()
        except Exception as error:
            second_errors.append(error)
        assert second_store.get_source_cursor("telegram") is None
        release.set()
        first_thread.join(timeout=2)

        assert not first_thread.is_alive()
        assert len(sink_calls) == 1
        assert second_errors and "claimed" in str(second_errors[0])
        assert first_cursor == InboxCursor("batch-c1", source="telegram")
        assert second_store.get_source_cursor("telegram") == InboxCursor("batch-c1", source="telegram")


def test_late_completion_cannot_finalize_a_reclaimed_epoch(tmp_path) -> None:
    page = ReadOnlyPage(
        items=(TelegramMcpPreview("chat", "1", "hello", cursor="item-c1"),),
        next_cursor="batch-c1",
        capabilities=frozenset({Capability.POLL}),
    )
    started = threading.Event()
    release = threading.Event()
    first_errors: list[Exception] = []
    sink_calls: list[str] = []

    def slow_sink(ref: str) -> None:
        sink_calls.append(ref)
        started.set()
        assert release.wait(2)

    first_cursor: InboxCursor | None = None

    def run_first() -> None:
        nonlocal first_cursor
        with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as first_store:
            first = build_telegram_watch(
                first_store,
                lambda _cursor, _limit: page,
                slow_sink,
                wake_lease_seconds=0.02,
            )
            try:
                first.poll_once()
            except Exception as error:
                first_errors.append(error)
            first_cursor = first_store.get_source_cursor("telegram")

    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as second_store:
        first_thread = threading.Thread(target=run_first)
        first_thread.start()
        assert started.wait(2)
        time.sleep(0.05)
        second = build_telegram_watch(
            second_store,
            lambda _cursor, _limit: page,
            sink_calls.append,
            wake_lease_seconds=0.02,
        )
        second_result = second.poll_once()
        release.set()
        first_thread.join(timeout=2)

        assert not first_thread.is_alive()
        assert second_result.emitted_refs == ("inbox://telegram/chat:1",)
        assert first_errors and "replaced" in str(first_errors[0])
        assert second_store.get_source_cursor("telegram") == InboxCursor("batch-c1", source="telegram")
        assert sink_calls == ["inbox://telegram/chat:1", "inbox://telegram/chat:1"]


def test_watch_retries_after_new_instance(tmp_path) -> None:
    page = ReadOnlyPage(
        items=(TelegramMcpPreview("chat", "9", "restart"),),
        next_cursor="c9",
        capabilities=frozenset({Capability.POLL}),
    )
    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        first = build_telegram_watch(store, lambda _cursor, _limit: page, lambda _ref: (_ for _ in ()).throw(RuntimeError("wake down")))
        with pytest.raises(RuntimeError, match="wake down"):
            first.poll_once()
        second_seen: list[str] = []
        second = build_telegram_watch(store, lambda _cursor, _limit: page, second_seen.append)
        result = second.poll_once()
        assert result.emitted_refs == ("inbox://telegram/chat:9",)
        assert second_seen == ["inbox://telegram/chat:9"]
        assert store.get_source_cursor("telegram") == InboxCursor("c9", source="telegram")


def test_watch_rejects_unbounded_opaque_ref(tmp_path) -> None:
    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        adapter = type("Adapter", (), {"manifest": type("Manifest", (), {"source": "telegram"})()})()
        watch = SecretaryWatch(store, adapter, lambda _ref: None, max_ref_length=32)
        with pytest.raises(ValueError, match="exceeds"):
            watch._ref(type("Identity", (), {"source": "telegram", "item_id": "x" * 100})())


def test_configured_watch_requires_explicit_dm_and_group_allowlist(tmp_path) -> None:
    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        with pytest.raises(RuntimeError, match="chat-kind preflight"):
            build_configured_telegram_watch(
                store,
                lambda _cursor, _limit: ReadOnlyPage(),
                environment={
                    "UNIVERSAL_INBOX_AGENT_HERDER_MCP_URL": "http://127.0.0.1:18787/mcp",
                    "UNIVERSAL_INBOX_TELEGRAM_DM_CHAT_ID": "dm-1",
                    "UNIVERSAL_INBOX_TELEGRAM_GROUP_CHAT_ID": "group-1",
                },
            )


def test_configured_watch_rejects_wrong_chat_kinds(tmp_path) -> None:
    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        with pytest.raises(RuntimeError, match="DM preflight"):
            build_configured_telegram_watch(
                store,
                lambda _cursor, _limit: ReadOnlyPage(),
                environment={
                    "UNIVERSAL_INBOX_AGENT_HERDER_MCP_URL": "http://127.0.0.1:18787/mcp",
                    "UNIVERSAL_INBOX_TELEGRAM_DM_CHAT_ID": "chat-1",
                    "UNIVERSAL_INBOX_TELEGRAM_GROUP_CHAT_ID": "chat-2",
                },
                chat_kind_reader=lambda _chat_id: "group",
            )


def test_configured_watch_accepts_verified_dm_and_group_pair(tmp_path) -> None:
    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        watch = build_configured_telegram_watch(
            store,
            lambda _cursor, _limit: ReadOnlyPage(),
            environment={
                "UNIVERSAL_INBOX_AGENT_HERDER_MCP_URL": "http://127.0.0.1:18787/mcp",
                "UNIVERSAL_INBOX_TELEGRAM_DM_CHAT_ID": "dm-1",
                "UNIVERSAL_INBOX_TELEGRAM_GROUP_CHAT_ID": "group-1",
            },
            chat_kind_reader=lambda chat_id: "dm" if chat_id == "dm-1" else "group",
        )
        assert watch is not None


def test_watch_runner_retries_transient_wake_failure() -> None:
    stop_event = threading.Event()
    attempts = 0
    errors: list[Exception] = []

    class FakeWatch:
        def poll_once(self, *, limit: int) -> None:
            nonlocal attempts
            assert limit == 7
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary wake outage")
            stop_event.set()

    run_telegram_watch(
        FakeWatch(),
        stop_event=stop_event,
        interval_seconds=0.001,
        limit=7,
        on_error=errors.append,
    )

    assert attempts == 2
    assert [str(error) for error in errors] == ["temporary wake outage"]
