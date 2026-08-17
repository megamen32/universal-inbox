from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from universal_inbox.noticeplace_sink import NoticePlaceReceipt
from universal_inbox.contracts import InboxItem, ItemIdentity
from universal_inbox.store import SQLiteInboxStore
from universal_inbox.webhook_ingress import build_webhook_handler
from universal_inbox.webhook_runtime import build_configured_webhook_handler


def test_webhook_ingress_accepts_all_message_spokes_and_returns_outbox_receipt(tmp_path) -> None:
    receipts = []

    class Sink:
        def __call__(self, ref):
            receipts.append(ref)
            return (NoticePlaceReceipt("evt_1", "inc_1", "dlv_1"), "conv_1")

    db_path = tmp_path / "inbox.sqlite3"
    with SQLiteInboxStore(db_path):
        pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_webhook_handler(db_path, "ingress-token", lambda _store: Sink()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for source in ("telegram", "matrix", "whatsapp", "vk", "phone"):
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/inbox/messages",
                data=json.dumps({
                    "schema": "universal.inbox.message.v1",
                    "source": source,
                    "message_id": f"{source}-1",
                    "sender": "operator",
                    "body": f"hello from {source}",
                }).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer ingress-token"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                assert response.status == 202
                result = json.loads(response.read())
                assert result["delivery_id"] == "dlv_1"
        with SQLiteInboxStore(db_path) as store:
            assert store.counts()["items"] == 5
            assert store.get(ItemIdentity("vk", "vk-1")).sender == "operator"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert receipts == [
        "inbox://telegram/telegram-1",
        "inbox://matrix/matrix-1",
        "inbox://whatsapp/whatsapp-1",
        "inbox://vk/vk-1",
        "inbox://phone/phone-1",
    ]


def test_webhook_ingress_rejects_unsupported_sources_and_bad_auth(tmp_path) -> None:
    db_path = tmp_path / "inbox.sqlite3"
    with SQLiteInboxStore(db_path):
        pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_webhook_handler(db_path, "ingress-token", lambda _store: (lambda _ref: None)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for token, source, expected in (("wrong", "vk", 401), ("ingress-token", "email", 400)):
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/inbox/messages",
                data=json.dumps({
                    "schema": "universal.inbox.message.v1",
                    "source": source,
                    "message_id": "1",
                    "body": "hello",
                }).encode(),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                method="POST",
            )
            try:
                urllib.request.urlopen(request, timeout=2)
            except urllib.error.HTTPError as error:
                assert error.code == expected
            else:
                raise AssertionError("invalid ingress was accepted")
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_sender_metadata_is_backward_compatible_with_existing_canonical_item(tmp_path) -> None:
    identity = ItemIdentity("telegram", "legacy-1")
    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        store.ingest(InboxItem(identity, title="anna", body="hello"))
        row = store._connection.execute("SELECT payload_json FROM items WHERE source=? AND item_id=?", ("telegram", "legacy-1")).fetchone()
        payload = json.loads(row["payload_json"])
        del payload["sender"]
        store._connection.execute("UPDATE items SET payload_json=? WHERE source=? AND item_id=?", (json.dumps(payload, sort_keys=True, separators=(",", ":")), "telegram", "legacy-1"))
        assert store.ingest(InboxItem(identity, title="anna", sender="anna", body="hello")) is False


def test_webhook_ingress_ignores_operator_owned_sender_ids_before_routing(tmp_path) -> None:
    db_path = tmp_path / "inbox.sqlite3"
    routed = []
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        build_webhook_handler(
            db_path,
            "ingress-token",
            lambda _store: routed.append,
            ignored_senders={"whatsapp": {"outbox-bot"}, "vk": {"relay-user"}},
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/inbox/messages",
            data=json.dumps({
                "schema": "universal.inbox.message.v1",
                "source": "whatsapp",
                "message_id": "own-1",
                "sender": "outbox-bot",
                "body": "must not loop",
            }).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer ingress-token"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            result = json.loads(response.read())
            assert response.status == 202
            assert result["ignored"] is True
        with SQLiteInboxStore(db_path) as store:
            assert store.counts()["items"] == 0
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    assert routed == []


def test_configured_webhook_runtime_requires_both_ingress_and_outbox_auth(tmp_path) -> None:
    required = {
        "UNIVERSAL_INBOX_WEBHOOK_TOKEN": "ingress-token",
        "UNIVERSAL_INBOX_NOTICEPLACE_EVENT_URL": "http://127.0.0.1:8091/v1/events",
        "UNIVERSAL_INBOX_NOTICEPLACE_TOKEN": "outbox-token",
    }
    for missing in required:
        environment = {key: value for key, value in required.items() if key != missing}
        try:
            build_configured_webhook_handler(tmp_path / "inbox.sqlite3", environment=environment)
        except RuntimeError as error:
            assert missing in str(error)
        else:
            raise AssertionError(f"missing {missing} was accepted")

    handler = build_configured_webhook_handler(tmp_path / "inbox.sqlite3", environment=required)
    assert handler is not None
