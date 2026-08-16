from __future__ import annotations

import json
import socket
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from universal_inbox.adapters.overpod_telegram import (
    OverpodTelegramIpcClient,
    OverpodTelegramIpcReader,
    parse_overpod_chat_kind,
)
from universal_inbox.adapter import PermanentAdapterError
from universal_inbox.contracts import InboxCursor
from universal_inbox.store import SQLiteInboxStore
from universal_inbox.telegram_runtime import build_overpod_configured_telegram_watch


def _tool_result(value: object) -> dict[str, object]:
    return {"content": [{"type": "text", "text": json.dumps(value)}]}


class FakeOverpodClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.update_payloads: list[dict[str, object]] = []

    def call(self, tool: str, args: dict[str, object]) -> dict[str, object]:
        self.calls.append((tool, args))
        if tool == "telegram-status":
            return {"content": [{"type": "text", "text": "Connected as @secretary (id: 12345)"}]}
        if tool == "telegram-get-chat-info":
            chat_id = args["chatId"]
            kind = "private" if chat_id == "900" else "group"
            return {"content": [{"type": "text", "text": f"Name: test\nID: {chat_id}\nType: {kind}"}]}
        if tool == "telegram-get-state":
            return _tool_result({"pts": 10, "qts": 0, "date": 100, "seq": 1, "unreadCount": 0})
        if tool == "telegram-get-updates":
            return _tool_result(self.update_payloads.pop(0))
        raise AssertionError(f"unexpected tool: {tool}")


def test_reader_uses_overpod_global_cursor_and_filters_to_dm_and_group() -> None:
    client = FakeOverpodClient()
    client.update_payloads.append(
        {
            "state": {"pts": 12, "qts": 0, "date": 102, "seq": 2},
            "isFinal": True,
            "newMessages": [
                {
                    "id": 1,
                    "peer": {"kind": "channel", "id": "4330127635"},
                    "fromId": {"kind": "user", "id": "777"},
                    "date": 101,
                    "text": "group message",
                    "isService": False,
                },
                {
                    "id": 2,
                    "peer": {"kind": "user", "id": "900"},
                    "fromId": {"kind": "user", "id": "900"},
                    "date": 101,
                    "text": "dm message",
                    "isService": False,
                },
                {
                    "id": 3,
                    "peer": {"kind": "channel", "id": "4330127635"},
                    "fromId": {"kind": "user", "id": "12345"},
                    "date": 101,
                    "text": "own message must not wake",
                    "isService": False,
                },
                {
                    "id": 4,
                    "peer": {"kind": "user", "id": "901"},
                    "fromId": {"kind": "user", "id": "901"},
                    "date": 101,
                    "text": "unselected dm",
                    "isService": False,
                },
            ],
            "deletedMessageIds": [],
            "otherUpdates": [],
        }
    )

    reader = OverpodTelegramIpcReader(
        client,
        dm_chat_id="900",
        group_chat_id="4330127635",
    )

    page = reader(None, limit=10)

    assert [(item.chat_id, item.message_id, item.text) for item in page.items] == [
        ("-1004330127635", "1", "group message"),
        ("900", "2", "dm message"),
    ]
    assert page.next_cursor is not None
    assert json.loads(page.next_cursor)["state"]["pts"] == 12
    assert [tool for tool, _args in client.calls] == [
        "telegram-status",
        "telegram-get-state",
        "telegram-get-updates",
    ]
    assert client.calls[-1][1]["pts"] == 10


def test_reader_drains_non_final_overpod_update_slices() -> None:
    client = FakeOverpodClient()
    client.update_payloads.extend(
        [
            {
                "state": {"pts": 11, "qts": 0, "date": 101, "seq": 1},
                "isFinal": False,
                "newMessages": [],
                "deletedMessageIds": [],
                "otherUpdates": [],
            },
            {
                "state": {"pts": 12, "qts": 0, "date": 102, "seq": 2},
                "isFinal": True,
                "newMessages": [
                    {
                        "id": 5,
                        "peer": {"kind": "channel", "id": "4330127635"},
                        "fromId": {"kind": "user", "id": "777"},
                        "date": 102,
                        "text": "after slice",
                        "isService": False,
                    }
                ],
                "deletedMessageIds": [],
                "otherUpdates": [],
            },
        ]
    )

    reader = OverpodTelegramIpcReader(
        client,
        dm_chat_id="900",
        group_chat_id="-1004330127635",
    )
    page = reader(
        InboxCursor(
            json.dumps({"version": 1, "state": {"pts": 10, "qts": 0, "date": 100}}),
            source="telegram",
        ),
        10,
    )

    assert [item.message_id for item in page.items] == ["5"]
    assert len([tool for tool, _args in client.calls if tool == "telegram-get-updates"]) == 2
    assert client.calls[-1][1]["pts"] == 11


def test_chat_kind_parser_maps_overpod_types() -> None:
    assert parse_overpod_chat_kind("Name: Me\nID: 900\nType: private") == "dm"
    assert parse_overpod_chat_kind("Name: ИИ Фронтир\nID: 4330127635\nType: group") == "group"
    with pytest.raises(ValueError, match="chat type"):
        parse_overpod_chat_kind("Name: unknown")


def test_overpod_factory_uses_daemon_reader_and_canonical_group_id(tmp_path: Path) -> None:
    client = FakeOverpodClient()
    environment = {
        "UNIVERSAL_INBOX_AGENT_HERDER_MCP_URL": "http://127.0.0.1:18787/mcp",
        "UNIVERSAL_INBOX_TELEGRAM_DM_CHAT_ID": "900",
        "UNIVERSAL_INBOX_TELEGRAM_GROUP_CHAT_ID": "4330127635",
    }

    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        watch = build_overpod_configured_telegram_watch(store, environment=environment, ipc_client=client)

    assert watch._adapter.manifest.adapter_id == "telegram-overpod-daemon"  # type: ignore[attr-defined]
    assert ("telegram-get-chat-info", {"chatId": "-1004330127635"}) in client.calls


def test_ipc_client_round_trips_one_newline_delimited_tool_call() -> None:
    client_socket, server_socket = socket.socketpair()
    received: list[dict[str, object]] = []

    def serve_once() -> None:
        with server_socket:
            data = b""
            while not data.endswith(b"\n"):
                data += server_socket.recv(4096)
            request = json.loads(data.decode("utf-8"))
            received.append(request)
            response = {
                "type": "tool_response",
                "id": request["id"],
                "result": _tool_result({"ok": True}),
            }
            server_socket.sendall((json.dumps(response) + "\n").encode("utf-8"))

    thread = threading.Thread(target=serve_once)
    thread.start()
    client = OverpodTelegramIpcClient(
        "/unused/daemon.sock",
        connect_fn=lambda _path, _timeout: client_socket,
    )

    result = client.call("telegram-get-state", {})

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert result == _tool_result({"ok": True})
    assert received[0]["type"] == "tool"
    assert received[0]["tool"] == "telegram-get-state"


def test_ipc_client_rejects_outbound_tools_before_opening_socket() -> None:
    opened = False

    def connect(_path: str, _timeout: float) -> socket.socket:
        nonlocal opened
        opened = True
        raise AssertionError("socket must not open for an outbound tool")

    client = OverpodTelegramIpcClient("/unused/daemon.sock", connect_fn=connect)

    with pytest.raises(PermanentAdapterError, match="read-only"):
        client.call("telegram-send-message", {"chatId": "900", "text": "must not send"})
    assert opened is False
