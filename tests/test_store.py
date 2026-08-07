from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from universal_inbox.contracts import ActionReceipt, ExplicitAction, InboxCursor, InboxItem, ItemIdentity
from universal_inbox.store import ItemConflictError, SQLiteInboxStore


def item(*, body: str = "Orchid status", cursor: str = "c-1") -> InboxItem:
    return InboxItem(
        identity=ItemIdentity("telegram", "message-1"),
        title="Inbox item",
        body=body,
        cursor=InboxCursor(cursor, source="telegram"),
    )


class SQLiteInboxStoreTest(unittest.TestCase):
    def test_replay_with_newer_cursor_is_a_noop_and_advances_source_cursor(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "inbox.sqlite3"
            store = SQLiteInboxStore(path)
            self.assertTrue(store.ingest(item()))
            self.assertFalse(store.ingest(item(cursor="c-2")))
            self.assertEqual(store.counts(), {"items": 1, "receipts": 0, "sources": 1})
            store.close()
            reopened = SQLiteInboxStore(path)
            self.assertEqual(reopened.get_source_cursor("telegram"), InboxCursor("c-2", source="telegram"))
            self.assertEqual([row.identity.item_id for row in reopened.search("orchid")], ["message-1"])

    def test_collision_and_receipt_are_idempotent(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            store = SQLiteInboxStore(Path(directory) / "inbox.sqlite3")
            self.assertTrue(store.ingest(item()))
            with self.assertRaises(ItemConflictError):
                store.ingest(item(body="different"))
            action = ExplicitAction(identity=ItemIdentity("telegram", "message-1"), kind="download")
            receipt = ActionReceipt(action=action, accepted=True, receipt_id="r-1")
            self.assertTrue(store.record_receipt(receipt))
            self.assertFalse(store.record_receipt(receipt))

    def test_rejected_receipt_is_refused_and_not_persisted(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            store = SQLiteInboxStore(Path(directory) / "inbox.sqlite3")
            action = ExplicitAction(identity=ItemIdentity("telegram", "message-1"), kind="download")
            receipt = ActionReceipt(action=action, accepted=False, receipt_id="r-2")
            with self.assertRaises(ValueError):
                store.record_receipt(receipt)
            self.assertEqual(store.counts(), {"items": 0, "receipts": 0, "sources": 0})


if __name__ == "__main__":
    unittest.main()
