from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from universal_inbox.adapter import AdapterManifest
from universal_inbox.contracts import Capability, InboxCursor, InboxItem, ItemIdentity, PollBatch, SourceStatus
from universal_inbox.mcp_surface import CoreMcpSurface
from universal_inbox.polling import PollingCoordinator
from universal_inbox.registry import AdapterRegistry
from universal_inbox.store import SQLiteInboxStore


class FixtureAdapter:
    def __init__(self) -> None:
        self.manifest = AdapterManifest(
            adapter_id="fixture-adapter",
            source="fixture",
            capabilities=frozenset({Capability.POLL, Capability.SEARCH}),
        )
        self._poll_count = 0
        self._cursor: InboxCursor | None = None

    def status(self) -> SourceStatus:
        return SourceStatus(
            source=self.manifest.source,
            adapter_id=self.manifest.adapter_id,
            cursor=self._cursor,
            item_count=self._poll_count,
        )

    def poll(self, cursor: InboxCursor | None, *, limit: int) -> PollBatch:
        assert limit > 0
        assert cursor == self._cursor
        self._poll_count += 1
        item = InboxItem(
            identity=ItemIdentity(self.manifest.source, f"fixture-{self._poll_count}"),
            title="Orchid note",
            body=f"fixture poll #{self._poll_count}",
            cursor=InboxCursor(f"cursor-{self._poll_count}", source=self.manifest.source),
        )
        self._cursor = item.cursor
        return PollBatch(
            items=(item,),
            next_cursor=item.cursor,
            capabilities=frozenset({Capability.POLL, Capability.SEARCH}),
        )

    def get(self, identity: ItemIdentity) -> InboxItem | None:
        return None

    def execute(self, action):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_surface_tools_list_call_poll_search_status_and_manifests(tmp_path: Path) -> None:
    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        registry = AdapterRegistry([FixtureAdapter()])
        coordinator = PollingCoordinator(store, registry)
        surface = CoreMcpSurface(store, registry=registry, coordinator=coordinator)

        assert [tool["name"] for tool in surface.tool_manifest()["tools"]] == [
            "inbox.search",
            "inbox.digest_candidates",
            "inbox.get",
            "inbox.sources.status",
            "inbox.source_status",
            "inbox.status",
            "inbox.adapters.manifests",
            "inbox.adapter_manifests",
            "inbox.poll_once",
            "inbox.poll_all",
        ]
        assert surface.dispatch("tools/list", {}) == surface.tool_manifest()

        poll_once = surface.dispatch(
            "tools/call",
            {
                "name": "inbox.poll_once",
                "arguments": {
                    "adapter_id": "fixture-adapter",
                    "limit": 5,
                    "request_id": "req-poll-1",
                },
            },
        )
        assert poll_once["ok"] is True
        assert poll_once["attempt"]["request_id"] == "req-poll-1"
        assert poll_once["attempt"]["accepted_item_ids"] == [
            {"source": "fixture", "item_id": "fixture-1"}
        ]

        search = surface.dispatch("tools/call", {"name": "inbox.search", "arguments": {"query": "orchid", "limit": 5}})
        assert search["ok"] is True
        assert search["items"][0]["ref"] == "inbox://fixture/fixture-1"
        assert search["items"][0]["title"] == "Orchid note"

        status = surface.dispatch("tools/call", {"name": "inbox.sources.status", "arguments": {"source": "fixture"}})
        assert status["ok"] is True
        assert status["status"]["adapter_id"] == "fixture-adapter"
        assert status["status"]["cursor"] == {"value": "cursor-1", "source": "fixture"}
        assert status["status"]["item_count"] == 1

        manifests = surface.dispatch("inbox.adapters.manifests", {})
        assert manifests["ok"] is True
        assert manifests["manifests"] == [
            {
                "adapter_id": "fixture-adapter",
                "source": "fixture",
                "capabilities": ["poll", "search"],
            }
        ]

        poll_all = surface.dispatch(
            "tools/call",
            {
                "name": "inbox.poll_all",
                "arguments": {"limit": 5, "request_ids": {"fixture-adapter": "req-poll-2"}},
            },
        )
        assert poll_all["ok"] is True
        assert len(poll_all["run"]["attempts"]) == 1
        assert poll_all["run"]["attempts"][0]["request_id"] == "req-poll-2"
