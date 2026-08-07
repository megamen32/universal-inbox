from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from universal_inbox.contracts import InboxItem, ItemIdentity
from universal_inbox.mcp_surface import CoreMcpSurface
from universal_inbox.store import SQLiteInboxStore


def test_search_and_digest_return_bounded_provenance_facts(tmp_path: Path) -> None:
    store = SQLiteInboxStore(tmp_path / "inbox.sqlite3")
    store.ingest(
        InboxItem(
            identity=ItemIdentity("telegram", "m-1"),
            title="Orchid follow-up",
            body="A bounded preview",
        )
    )
    surface = CoreMcpSurface(store)

    search = surface.dispatch("inbox.search", {"query": "orchid", "limit": 5})
    digest = surface.dispatch("inbox.digest_candidates", {"limit": 5})

    assert search["ok"] is True
    assert digest["ok"] is True
    assert search["items"][0]["ref"] == "inbox://telegram/m-1"
    assert digest["items"][0]["source"] == "telegram"
    assert "action" not in search["items"][0]


def test_unknown_or_invalid_commands_fail_closed(tmp_path: Path) -> None:
    surface = CoreMcpSurface(SQLiteInboxStore(tmp_path / "inbox.sqlite3"))
    assert surface.dispatch("inbox.send", {}) == {"ok": False, "error": "unknown_tool"}
    assert surface.dispatch("inbox.search", {"query": ""}) == {"ok": False, "error": "invalid_arguments"}
