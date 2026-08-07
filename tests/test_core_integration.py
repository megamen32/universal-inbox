from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from universal_inbox.contracts import InboxCursor, InboxItem, ItemIdentity
from universal_inbox.store import SQLiteInboxStore


def test_canonical_inbox_item_survives_reopen_search_and_dedupe(tmp_path: Path) -> None:
    path = tmp_path / "inbox.sqlite3"
    item = InboxItem(
        identity=ItemIdentity(source="telegram", item_id="msg-9"),
        title="Invoice follow-up",
        body="The orchid shipment needs approval before Friday.",
        cursor=InboxCursor(value="cursor-9", source="telegram"),
    )

    store = SQLiteInboxStore(path)

    assert store.ingest(item) is True
    assert [row.identity.item_id for row in store.search("orchid")] == ["msg-9"]

    store.close()

    reopened = SQLiteInboxStore(path)

    assert [row.identity.item_id for row in reopened.search("orchid")] == ["msg-9"]
    assert reopened.ingest(item) is False
    assert reopened.get_source_cursor("telegram") == InboxCursor(
        value="cursor-9", source="telegram"
    )
