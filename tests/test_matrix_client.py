from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from universal_inbox.adapters.matrix_client import MatrixClientReader, MatrixReadAdapter
from universal_inbox.adapter import TransientAdapterError


def test_matrix_reader_maps_text_events_after_baseline_cursor() -> None:
    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def read() -> bytes:
            return json.dumps({
                "next_batch": "s2",
                "rooms": {"join": {
                    "!ops:example.org": {"timeline": {"events": [
                        {"type": "m.room.message", "event_id": "$1", "sender": "@alice:example.org", "content": {"msgtype": "m.text", "body": "hello"}},
                        {"type": "m.room.message", "event_id": "$2", "sender": "@relay:example.org", "content": {"msgtype": "m.text", "body": "loop"}},
                        {"type": "m.room.member", "event_id": "$3", "sender": "@alice:example.org", "content": {}},
                    ]}},
                }},
            }).encode()

    def runner(request, *, timeout):
        requests.append((request, timeout))
        return Response()

    reader = MatrixClientReader(
        "https://matrix.example.org",
        "matrix-token",
        allowed_room_ids=("!ops:example.org",),
        own_user_id="@relay:example.org",
        runner=runner,
    )
    from universal_inbox.contracts import InboxCursor

    cursor = InboxCursor("s1", source="matrix")
    page = reader(cursor, 10)
    adapter = MatrixReadAdapter(adapter_id="matrix-client", reader=reader, allowed_room_ids=("!ops:example.org",))
    batch = adapter.poll(cursor, limit=10)

    assert [(item.room_id, item.event_id, item.body) for item in page.items] == [("!ops:example.org", "$1", "hello")]
    assert page.next_cursor == "s2"
    assert batch.items[0].identity.item_id == "!ops:example.org:$1"
    assert batch.items[0].title == "@alice:example.org"
    assert requests[0][0].get_header("Authorization") == "Bearer matrix-token"
    assert "timeout=0" in requests[0][0].full_url
    assert "since=s1" in requests[0][0].full_url


def test_matrix_reader_initial_sync_only_establishes_cursor_without_relaying_history() -> None:
    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def read() -> bytes:
            return b'{"next_batch":"s3","rooms":{"join":{"!other:example.org":{"timeline":{"events":[{"type":"m.room.message","event_id":"$9","sender":"@bob:example.org","content":{"msgtype":"m.text","body":"skip"}}]}}}}}'

    def runner(request, *, timeout):
        requests.append((request, timeout))
        return Response()

    reader = MatrixClientReader(
        "https://matrix.example.org",
        "matrix-token",
        allowed_room_ids=("!ops:example.org",),
        own_user_id="@relay:example.org",
        runner=runner,
    )
    page = reader(None, 10)

    assert page.items == ()
    assert page.next_cursor == "s3"
    assert "since=" not in requests[0][0].full_url


def test_matrix_reader_does_not_advance_cursor_when_batch_exceeds_limit() -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def read() -> bytes:
            return json.dumps({
                "next_batch": "s2",
                "rooms": {"join": {"!ops:example.org": {"timeline": {"events": [
                    {"type": "m.room.message", "event_id": "$1", "sender": "@alice:example.org", "content": {"msgtype": "m.text", "body": "one"}},
                    {"type": "m.room.message", "event_id": "$2", "sender": "@bob:example.org", "content": {"msgtype": "m.text", "body": "two"}},
                ]}}}},
            }).encode()

    reader = MatrixClientReader(
        "https://matrix.example.org",
        "matrix-token",
        allowed_room_ids=("!ops:example.org",),
        own_user_id="@relay:example.org",
        runner=lambda *_args, **_kwargs: Response(),
    )
    from universal_inbox.contracts import InboxCursor

    with pytest.raises(TransientAdapterError, match="more messages"):
        reader(InboxCursor("s1", source="matrix"), 1)


def test_matrix_reader_rejects_limited_timeline_without_advancing_cursor() -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def read() -> bytes:
            return json.dumps({
                "next_batch": "s3",
                "rooms": {"join": {"!ops:example.org": {"timeline": {
                    "limited": True,
                    "prev_batch": "t-backfill",
                    "events": [{"type": "m.room.message", "event_id": "$3", "sender": "@alice:example.org", "content": {"msgtype": "m.text", "body": "tail"}}],
                }}}},
            }).encode()

    reader = MatrixClientReader(
        "https://matrix.example.org",
        "matrix-token",
        allowed_room_ids=("!ops:example.org",),
        own_user_id="@relay:example.org",
        runner=lambda *_args, **_kwargs: Response(),
    )
    from universal_inbox.contracts import InboxCursor

    with pytest.raises(TransientAdapterError, match="limited timeline"):
        reader(InboxCursor("s2", source="matrix"), 10)
