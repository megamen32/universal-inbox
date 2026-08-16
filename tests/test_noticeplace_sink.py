from __future__ import annotations

import json
import sys
import threading
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from universal_inbox.contracts import InboxItem, ItemIdentity
from universal_inbox.noticeplace_sink import NoticePlaceInboxSink, build_routed_noticeplace_sink
from universal_inbox.noticeplace_runtime import build_noticeplace_matrix_watch, build_noticeplace_telegram_watch, run_noticeplace_watch
from universal_inbox.store import SQLiteInboxStore


def test_noticeplace_sink_maps_stored_item_to_route_neutral_event(tmp_path) -> None:
    requests = []

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def read() -> bytes:
            return b'{"event_id":"evt_1","incident_id":"inc_1","initial_delivery_id":"dlv_1"}'

    def runner(request, *, timeout):
        requests.append((request, timeout))
        return Response()

    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        store.ingest(InboxItem(ItemIdentity("telegram", "chat:1"), title="Nikita", body="hello"))
        sink = NoticePlaceInboxSink(
            store,
            "http://127.0.0.1:8091/v1/events",
            "producer-token",
            project="universal-inbox",
            recipient="operator",
            severity="notice",
            runner=runner,
        )
        first = sink("inbox://telegram/chat:1")
        second = sink("inbox://telegram/chat:1")

    payload = json.loads(requests[0][0].data)
    assert payload == {
        "schema": "notify.event.v1",
        "project": "universal-inbox",
        "recipient": "operator",
        "kind": "notification",
        "severity": "notice",
        "title": "Nikita",
        "body": "hello",
        "dedup_key": "inbox:telegram:chat:1",
        "correlation_id": "inbox://telegram/chat:1",
        "event_type": "inbox.message",
        "producer": "universal-inbox",
        "plugin": "telegram",
    }
    assert "target" not in payload
    assert requests[0][0].get_header("Authorization") == "Bearer producer-token"
    assert requests[0][0].get_header("Idempotency-key") == requests[1][0].get_header("Idempotency-key")
    assert first == second
    assert first.incident_id == "inc_1"
    assert first.delivery_id == "dlv_1"


def test_routed_noticeplace_sink_selects_scoped_token_by_source(tmp_path) -> None:
    requests = []

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def read():
            return b'{"event_id":"evt_1","incident_id":"inc_1","initial_delivery_id":"dlv_1"}'

    def runner(request, *, timeout):
        requests.append(request)
        return Response()

    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        store.ingest(InboxItem(ItemIdentity("telegram", "1"), body="from tg"))
        store.ingest(InboxItem(ItemIdentity("matrix", "1"), body="from matrix"))
        sink = build_routed_noticeplace_sink(
            store,
            "http://127.0.0.1:8091/v1/events",
            routes={
                "telegram": {"token": "to-matrix-token", "recipient": "matrix-route"},
                "matrix": {"token": "to-telegram-token", "recipient": "telegram-route"},
            },
            project="universal-inbox",
            runner=runner,
        )
        sink("inbox://telegram/1")
        sink("inbox://matrix/1")

    assert [request.get_header("Authorization") for request in requests] == [
        "Bearer to-matrix-token",
        "Bearer to-telegram-token",
    ]
    assert [json.loads(request.data)["recipient"] for request in requests] == ["matrix-route", "telegram-route"]


def test_noticeplace_runtime_requires_operator_owned_route_configuration(tmp_path) -> None:
    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        try:
            build_noticeplace_telegram_watch(store, environment={})
        except RuntimeError as error:
            assert "UNIVERSAL_INBOX_NOTICEPLACE_EVENT_URL" in str(error)
        else:
            raise AssertionError("missing NoticePlace route must fail closed")


def test_noticeplace_runtime_accepts_source_routing_registry_without_default_token(tmp_path) -> None:
    environment = {
        "UNIVERSAL_INBOX_NOTICEPLACE_EVENT_URL": "http://127.0.0.1:8091/v1/events",
        "UNIVERSAL_INBOX_NOTICEPLACE_ROUTES_JSON": json.dumps({
            "telegram": {"token": "to-matrix-token", "recipient": "matrix-route"},
        }),
        "UNIVERSAL_INBOX_TELEGRAM_DM_CHAT_ID": "900",
        "UNIVERSAL_INBOX_TELEGRAM_GROUP_CHAT_ID": "4330127635",
    }
    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        watch = build_noticeplace_telegram_watch(
            store,
            environment=environment,
            ipc_client=object(),
            telegram_reader=lambda _cursor, _limit: None,
        )
    assert watch is not None


def test_noticeplace_runtime_composes_overpod_reader_with_outbox_sink(tmp_path) -> None:
    calls = []

    class IpcClient:
        def call(self, tool, args):
            if tool == "telegram-get-chat-info":
                kind = "private" if args["chatId"] == "900" else "group"
                return {"content": [{"type": "text", "text": f"Type: {kind}"}]}
            raise AssertionError(f"unexpected preflight tool: {tool}")

    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        watch = build_noticeplace_telegram_watch(
            store,
            environment={
                "UNIVERSAL_INBOX_NOTICEPLACE_EVENT_URL": "http://127.0.0.1:8091/v1/events",
                "UNIVERSAL_INBOX_NOTICEPLACE_TOKEN": "producer-token",
                "UNIVERSAL_INBOX_NOTICEPLACE_PROJECT": "universal-inbox",
                "UNIVERSAL_INBOX_NOTICEPLACE_RECIPIENT": "operator",
                "UNIVERSAL_INBOX_TELEGRAM_DM_CHAT_ID": "900",
                "UNIVERSAL_INBOX_TELEGRAM_GROUP_CHAT_ID": "4330127635",
            },
            ipc_client=IpcClient(),
            http_runner=lambda request, *, timeout: calls.append((request, timeout)),
        )

    assert watch is not None


def test_noticeplace_runtime_composes_bot_api_reader_without_overpod(tmp_path) -> None:
    environment = {
        "UNIVERSAL_INBOX_NOTICEPLACE_EVENT_URL": "http://127.0.0.1:8091/v1/events",
        "UNIVERSAL_INBOX_NOTICEPLACE_TOKEN": "producer-token",
        "UNIVERSAL_INBOX_TELEGRAM_TRANSPORT": "bot-api",
        "UNIVERSAL_INBOX_TELEGRAM_SINGLE_READER": "true",
        "UNIVERSAL_INBOX_TELEGRAM_BOT_TOKEN": "telegram-token",
        "UNIVERSAL_INBOX_TELEGRAM_ACCOUNT_ID": "99",
        "UNIVERSAL_INBOX_TELEGRAM_DM_CHAT_ID": "900",
        "UNIVERSAL_INBOX_TELEGRAM_GROUP_CHAT_ID": "4330127635",
    }
    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        watch = build_noticeplace_telegram_watch(store, environment=environment)

    assert watch._adapter.manifest.adapter_id == "telegram-bot-api-noticeplace"  # type: ignore[attr-defined]


def test_noticeplace_runtime_rejects_second_bot_api_reader_without_ownership_gate(tmp_path) -> None:
    environment = {
        "UNIVERSAL_INBOX_NOTICEPLACE_EVENT_URL": "http://127.0.0.1:8091/v1/events",
        "UNIVERSAL_INBOX_NOTICEPLACE_TOKEN": "producer-token",
        "UNIVERSAL_INBOX_TELEGRAM_TRANSPORT": "bot-api",
        "UNIVERSAL_INBOX_TELEGRAM_BOT_TOKEN": "telegram-token",
        "UNIVERSAL_INBOX_TELEGRAM_ACCOUNT_ID": "99",
        "UNIVERSAL_INBOX_TELEGRAM_DM_CHAT_ID": "900",
        "UNIVERSAL_INBOX_TELEGRAM_GROUP_CHAT_ID": "4330127635",
    }
    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        try:
            build_noticeplace_telegram_watch(store, environment=environment)
        except RuntimeError as error:
            assert "single-reader" in str(error)
        else:
            raise AssertionError("a second Bot API reader was accepted without ownership attestation")


def test_noticeplace_telegram_runtime_filters_outbox_bot_sender_to_prevent_loops(tmp_path) -> None:
    from universal_inbox.adapters._read_only import ReadOnlyPage
    from universal_inbox.adapters.telegram_mcp import TelegramMcpPreview

    seen = []

    class ReaderClient:
        pass

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def read():
            return b'{"event_id":"evt_1","incident_id":"inc_1","initial_delivery_id":"dlv_1"}'

    page = ReadOnlyPage(
        items=(
            TelegramMcpPreview("900", "1", "real", sender="777", cursor="c1"),
            TelegramMcpPreview("900", "2", "bridged", sender="555", cursor="c1"),
        ),
        next_cursor="c1",
    )
    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        watch = build_noticeplace_telegram_watch(
            store,
            environment={
                "UNIVERSAL_INBOX_NOTICEPLACE_EVENT_URL": "http://127.0.0.1:8091/v1/events",
                "UNIVERSAL_INBOX_NOTICEPLACE_TOKEN": "producer-token",
                "UNIVERSAL_INBOX_TELEGRAM_DM_CHAT_ID": "900",
                "UNIVERSAL_INBOX_TELEGRAM_GROUP_CHAT_ID": "4330127635",
                "UNIVERSAL_INBOX_TELEGRAM_IGNORED_SENDER_IDS": "555, 556",
            },
            ipc_client=ReaderClient(),
            telegram_reader=lambda _cursor, _limit: page,
            http_runner=lambda request, *, timeout: (seen.append(json.loads(request.data)), Response())[1],
        )
        result = watch.poll_once()

    assert result.emitted_refs == ("inbox://telegram/900:1",)
    assert [payload["body"] for payload in seen] == ["real"]


def test_noticeplace_runtime_composes_matrix_reader_with_same_outbox_sink(tmp_path) -> None:
    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        watch = build_noticeplace_matrix_watch(
            store,
            environment={
                "UNIVERSAL_INBOX_NOTICEPLACE_EVENT_URL": "http://127.0.0.1:8091/v1/events",
                "UNIVERSAL_INBOX_NOTICEPLACE_TOKEN": "producer-token",
                "UNIVERSAL_INBOX_NOTICEPLACE_PROJECT": "universal-inbox",
                "UNIVERSAL_INBOX_NOTICEPLACE_RECIPIENT": "operator",
                "UNIVERSAL_INBOX_MATRIX_HOMESERVER": "https://matrix.example.org",
                "UNIVERSAL_INBOX_MATRIX_ACCESS_TOKEN": "matrix-token",
                "UNIVERSAL_INBOX_MATRIX_ROOM_IDS": "!ops:example.org,!dm:example.org",
                "UNIVERSAL_INBOX_MATRIX_USER_ID": "@relay:example.org",
            },
            matrix_runner=lambda request, *, timeout: None,
            http_runner=lambda request, *, timeout: None,
        )

    assert watch is not None


def test_noticeplace_runtime_polls_continuously_until_stopped() -> None:
    stop = threading.Event()
    calls = []

    class Watch:
        def poll_once(self, *, limit):
            calls.append(limit)
            if len(calls) == 2:
                stop.set()

    run_noticeplace_watch(Watch(), stop_event=stop, interval_seconds=0.001, limit=7)

    assert calls == [7, 7]


def test_noticeplace_runtime_delivers_new_message_after_store_reopen(tmp_path) -> None:
    from universal_inbox.adapters._read_only import ReadOnlyPage
    from universal_inbox.adapters.telegram_mcp import TelegramMcpPreview

    seen = []

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def read():
            return b'{"event_id":"evt_1","incident_id":"inc_1","initial_delivery_id":"dlv_1"}'

    environment = {
        "UNIVERSAL_INBOX_NOTICEPLACE_EVENT_URL": "http://127.0.0.1:8091/v1/events",
        "UNIVERSAL_INBOX_NOTICEPLACE_TOKEN": "producer-token",
        "UNIVERSAL_INBOX_TELEGRAM_DM_CHAT_ID": "900",
        "UNIVERSAL_INBOX_TELEGRAM_GROUP_CHAT_ID": "4330127635",
    }
    db_path = tmp_path / "inbox.sqlite3"
    for message_id, expected_cursor in (("1", None), ("2", "c1")):
        page = ReadOnlyPage(
            (TelegramMcpPreview("900", message_id, f"message-{message_id}", sender="777", cursor=f"c{message_id}"),),
            f"c{message_id}",
        )

        def reader(cursor, _limit, page=page, expected_cursor=expected_cursor):
            assert (cursor.value if cursor else None) == expected_cursor
            return page

        with SQLiteInboxStore(db_path) as store:
            watch = build_noticeplace_telegram_watch(
                store,
                environment=environment,
                ipc_client=object(),
                telegram_reader=reader,
                http_runner=lambda request, *, timeout: (seen.append(json.loads(request.data)), Response())[1],
            )
            watch.poll_once()

    assert [payload["body"] for payload in seen] == ["message-1", "message-2"]
