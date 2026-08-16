from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from universal_inbox.adapter import TransientAdapterError
from universal_inbox.adapters.telegram_bot_api import TelegramBotApiReader
from universal_inbox.contracts import InboxCursor


class Response:
    status = 200

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


def test_bot_api_reader_establishes_baseline_without_forwarding_history() -> None:
    requests = []

    def runner(request, *, timeout):
        requests.append((request, timeout))
        return Response({"ok": True, "result": [
            {"update_id": 40, "message": {"message_id": 5, "from": {"id": 7}, "chat": {"id": -1001}, "text": "old"}},
            {"update_id": 41, "callback_query": {"id": "skip"}},
        ]})

    reader = TelegramBotApiReader(
        "bot-token",
        allowed_chat_ids=("-1001",),
        own_bot_id="99",
        runner=runner,
    )

    page = reader(None, 10)

    assert page.items == ()
    assert page.next_cursor == "42"
    payload = dict(urllib.parse.parse_qsl(requests[0][0].data.decode()))
    assert payload == {"allowed_updates": "[\"message\",\"channel_post\"]", "limit": "100", "timeout": "0"}


def test_bot_api_reader_maps_allowlisted_text_and_advances_update_cursor() -> None:
    requests = []

    def runner(request, *, timeout):
        requests.append((request, timeout))
        return Response({"ok": True, "result": [
            {"update_id": 42, "message": {"message_id": 6, "from": {"id": 7}, "chat": {"id": -1001}, "text": "hello"}},
            {"update_id": 43, "message": {"message_id": 7, "from": {"id": 99, "is_bot": True}, "chat": {"id": -1001}, "text": "own"}},
            {"update_id": 44, "message": {"message_id": 8, "from": {"id": 8}, "chat": {"id": -1002}, "text": "other room"}},
            {"update_id": 45, "channel_post": {"message_id": 9, "sender_chat": {"id": -1003}, "chat": {"id": -1001}, "text": "channel post"}},
        ]})

    reader = TelegramBotApiReader(
        "bot-token",
        allowed_chat_ids=("-1001",),
        own_bot_id="99",
        ignored_sender_ids=("8",),
        runner=runner,
    )

    page = reader(InboxCursor("42", source="telegram"), 10)

    assert [(item.chat_id, item.message_id, item.sender, item.text) for item in page.items] == [
        ("-1001", "6", "7", "hello"),
        ("-1001", "9", "-1003", "channel post"),
    ]
    assert page.next_cursor == "46"
    assert dict(urllib.parse.parse_qsl(requests[0][0].data.decode()))["offset"] == "42"


def test_bot_api_reader_fails_closed_before_confirming_an_overflow_batch() -> None:
    def runner(_request, *, timeout):
        return Response({"ok": True, "result": [
            {"update_id": 50, "message": {"message_id": 10, "from": {"id": 7}, "chat": {"id": -1001}, "text": "one"}},
            {"update_id": 51, "message": {"message_id": 11, "from": {"id": 7}, "chat": {"id": -1001}, "text": "two"}},
        ]})

    reader = TelegramBotApiReader(
        "bot-token",
        allowed_chat_ids=("-1001",),
        own_bot_id="99",
        runner=runner,
    )

    with pytest.raises(TransientAdapterError, match="more messages"):
        reader(InboxCursor("50", source="telegram"), 1)
