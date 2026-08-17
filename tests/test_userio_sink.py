from __future__ import annotations

import json

from universal_inbox.contracts import InboxItem, ItemIdentity
from universal_inbox.noticeplace_runtime import _delivery_sink
from universal_inbox.store import SQLiteInboxStore
from universal_inbox.userio_sink import FanoutSink, UserIOInboxSink


def test_userio_sink_emits_canonical_message_with_configured_source_route(tmp_path) -> None:
    store = SQLiteInboxStore(tmp_path / "inbox.sqlite3")
    item = InboxItem(ItemIdentity("matrix", "room:event"), body="hello", sender="@anna:example.org")
    store.ingest(item)
    requests = []

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def read() -> bytes:
            return b'{"conversation_id":"conv_1"}'

    def runner(request, *, timeout):
        requests.append((request, timeout))
        return Response()

    sink = UserIOInboxSink(store, "http://127.0.0.1:18093", "userio-token", route_for_source={"matrix": "matrix-reply"}, runner=runner)

    assert sink("inbox://matrix/room:event") == "conv_1"
    payload = json.loads(requests[0][0].data)
    assert payload == {"route_id": "matrix-reply", "message": {"schema": "universal.inbox.message.v1", "source": "matrix", "message_id": "room:event", "sender": "@anna:example.org", "body": "hello"}}
    assert requests[0][0].get_header("Authorization") == "Bearer userio-token"


def test_fanout_requires_every_consumer_to_acknowledge() -> None:
    calls = []

    def first(ref):
        calls.append(("first", ref))
        return "one"

    def second(ref):
        calls.append(("second", ref))
        return "two"

    assert FanoutSink(first, second)("inbox://telegram/1") == ("one", "two")
    assert calls == [("first", "inbox://telegram/1"), ("second", "inbox://telegram/1")]


def test_runtime_fanout_delivers_same_inbox_item_to_outbox_and_userio(tmp_path) -> None:
    store = SQLiteInboxStore(tmp_path / "inbox.sqlite3")
    store.ingest(InboxItem(ItemIdentity("telegram", "chat:7"), body="hello", sender="chat"))
    requests = []

    class Response:
        status = 202

        def __init__(self, body):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return self._body

    def runner(request, *, timeout):
        requests.append((request, timeout))
        if request.full_url.startswith("http://noticeplace"):
            return Response(b'{"event_id":"evt","incident_id":"inc"}')
        return Response(b'{"conversation_id":"conv"}')

    sink = _delivery_sink(store, {
        "UNIVERSAL_INBOX_NOTICEPLACE_EVENT_URL": "http://noticeplace/v1/events",
        "UNIVERSAL_INBOX_NOTICEPLACE_TOKEN": "outbox-token",
        "UNIVERSAL_USERIO_INGRESS_URL": "http://userio",
        "UNIVERSAL_USERIO_INGRESS_TOKEN": "userio-token",
        "UNIVERSAL_USERIO_ROUTES_JSON": '{"telegram":"telegram-reply"}',
    }, runner=runner)

    sink("inbox://telegram/chat:7")

    assert [request.full_url for request, _ in requests] == ["http://noticeplace/v1/events", "http://userio/v1/messages"]
    assert json.loads(requests[1][0].data)["message"]["sender"] == "chat"
