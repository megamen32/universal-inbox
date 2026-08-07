from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from universal_inbox.contracts import (
    ActionReceipt,
    Capability,
    ContentState,
    ExplicitAction,
    InboxCursor,
    InboxItem,
    ItemIdentity,
    ItemRef,
    PollBatch,
)


class ContractsTests(unittest.TestCase):
    def test_identity_normalizes_and_is_hashable(self) -> None:
        identity_a = ItemIdentity(source=" Gmail ", item_id=" 123 ")
        identity_b = ItemIdentity(source="gmail", item_id="123")

        self.assertEqual(identity_a, identity_b)
        self.assertEqual(identity_a.source, "gmail")
        self.assertEqual(identity_a.item_id, "123")
        self.assertEqual(len({identity_a, identity_b}), 1)

    def test_ref_and_item_preserve_identity_and_content_state(self) -> None:
        identity = ItemIdentity(source="slack", item_id="abc")
        ref = ItemRef(identity=identity, label="thread")
        item = InboxItem(
            identity=identity,
            refs=(ref,),
            content_state=ContentState.PRESENT,
            title="A message",
        )

        self.assertEqual(item.identity, identity)
        self.assertEqual(item.refs, (ref,))
        self.assertEqual(item.content_state, ContentState.PRESENT)
        self.assertFalse(item.is_tombstoned)

    def test_cursor_and_poll_batch_keep_source_owned_values_opaque(self) -> None:
        cursor = InboxCursor(value="  next-page-token  ")
        batch = PollBatch(
            items=(),
            next_cursor=cursor,
            capabilities=frozenset({Capability.POLL}),
        )

        self.assertEqual(cursor.value, "next-page-token")
        self.assertEqual(batch.next_cursor, cursor)
        self.assertEqual(batch.capabilities, frozenset({Capability.POLL}))

    def test_explicit_action_and_receipt_require_matching_identity_and_kind(self) -> None:
        identity = ItemIdentity(source="notion", item_id="page-7")
        action = ExplicitAction(identity=identity, kind="archive")
        receipt = ActionReceipt(
            action=action,
            accepted=True,
            receipt_id="rcpt-1",
        )

        self.assertEqual(receipt.action.identity, identity)
        self.assertEqual(receipt.action.kind, "archive")
        self.assertTrue(receipt.accepted)
        self.assertEqual(receipt.receipt_id, "rcpt-1")

        with self.assertRaises(ValueError):
            ActionReceipt(
                action=ExplicitAction(
                    identity=ItemIdentity(source="notion", item_id="page-8"),
                    kind="archive",
                ),
                accepted=True,
                receipt_id="rcpt-2",
                item_identity=identity,
            )

    def test_invalid_tokens_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ItemIdentity(source="", item_id="x")
        with self.assertRaises(ValueError):
            InboxCursor(value="   ")
        with self.assertRaises(ValueError):
            ItemRef(identity=ItemIdentity(source="a", item_id="b"), label="  ")


if __name__ == "__main__":
    unittest.main()
