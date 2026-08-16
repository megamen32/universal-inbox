"""Dependency-free read-only Matrix Client-Server adapter."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from ..adapter import MalformedAdapterResponse, TransientAdapterError
from ..contracts import Capability, InboxCursor, InboxItem, ItemIdentity
from ._read_only import ReadOnlyInboxAdapter, ReadOnlyPage


@dataclass(frozen=True, slots=True)
class MatrixPreview:
    room_id: str
    event_id: str
    sender: str
    body: str
    cursor: str


def _matrix_item_mapper(record: MatrixPreview, source: str) -> InboxItem:
    return InboxItem(
        identity=ItemIdentity(source, f"{record.room_id}:{record.event_id}"),
        title=record.sender,
        body=record.body,
        cursor=InboxCursor(record.cursor, source=source),
    )


class MatrixClientReader:
    """Read bounded text events from allowlisted rooms through `/sync`."""

    def __init__(
        self,
        homeserver: str,
        token: str,
        *,
        allowed_room_ids,
        own_user_id: str,
        timeout_seconds: float = 30,
        runner: Any = urllib.request.urlopen,
    ) -> None:
        self._homeserver = homeserver.rstrip("/")
        self._token = token
        self._allowed_room_ids = frozenset(str(value).strip() for value in allowed_room_ids if str(value).strip())
        self._own_user_id = own_user_id.strip()
        self._timeout_seconds = timeout_seconds
        self._runner = runner
        if not self._homeserver or not self._token or not self._allowed_room_ids or not self._own_user_id:
            raise ValueError("Matrix reader requires homeserver, token, room allowlist, and own user id")

    def __call__(self, cursor: InboxCursor | None, limit: int) -> ReadOnlyPage[MatrixPreview]:
        if limit < 1:
            raise ValueError("limit must be positive")
        if cursor is not None and cursor.source not in {None, "matrix"}:
            raise ValueError("Matrix cursor source mismatch")
        query = {"timeout": "0"}
        if cursor is not None:
            query["since"] = cursor.value
        url = f"{self._homeserver}/_matrix/client/v3/sync?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}"})
        with self._runner(request, timeout=self._timeout_seconds) as response:
            if not 200 <= int(response.status) < 300:
                raise RuntimeError(f"Matrix sync returned HTTP {response.status}")
            try:
                result = json.loads(response.read())
            except json.JSONDecodeError as error:
                raise MalformedAdapterResponse("Matrix sync returned invalid JSON") from error
        next_batch = result.get("next_batch") if isinstance(result, dict) else None
        rooms = result.get("rooms", {}).get("join", {}) if isinstance(result, dict) else {}
        if not isinstance(next_batch, str) or not next_batch or not isinstance(rooms, dict):
            raise MalformedAdapterResponse("Matrix sync response is incomplete")
        if cursor is None:
            return ReadOnlyPage((), next_batch, frozenset({Capability.POLL}))
        items: list[MatrixPreview] = []
        overflow = False
        for room_id, room in rooms.items():
            if room_id not in self._allowed_room_ids or not isinstance(room, dict):
                continue
            timeline = room.get("timeline", {})
            if not isinstance(timeline, dict):
                raise MalformedAdapterResponse("Matrix timeline must be an object")
            if timeline.get("limited") is True:
                raise TransientAdapterError("Matrix sync returned a limited timeline; backfill is required")
            events = timeline.get("events", {})
            if not isinstance(events, list):
                raise MalformedAdapterResponse("Matrix timeline events must be a list")
            for event in events:
                if not isinstance(event, dict) or event.get("type") != "m.room.message" or event.get("sender") == self._own_user_id:
                    continue
                content = event.get("content")
                if not isinstance(content, dict) or content.get("msgtype") != "m.text" or not isinstance(content.get("body"), str):
                    continue
                event_id = event.get("event_id")
                sender = event.get("sender")
                if not isinstance(event_id, str) or not event_id or not isinstance(sender, str) or not sender:
                    raise MalformedAdapterResponse("Matrix text event has no identity")
                if len(items) >= limit:
                    overflow = True
                    break
                items.append(MatrixPreview(room_id, event_id, sender, content["body"], next_batch))
            if overflow:
                break
        if overflow:
            raise TransientAdapterError("Matrix sync returned more messages than the inbox limit")
        return ReadOnlyPage(tuple(items), next_batch, frozenset({Capability.POLL}))


class MatrixReadAdapter(ReadOnlyInboxAdapter[MatrixPreview]):
    def __init__(self, *, adapter_id: str, reader, allowed_room_ids=None) -> None:
        allowlist = None if allowed_room_ids is None else frozenset(
            str(room_id).strip() for room_id in allowed_room_ids if str(room_id).strip()
        )
        if allowed_room_ids is not None and not allowlist:
            raise ValueError("allowed_room_ids must contain at least one room id")

        def filtered_reader(cursor, limit):
            page = reader(cursor, limit)
            if allowlist is None:
                return page
            return ReadOnlyPage(
                tuple(item for item in page.items if item.room_id in allowlist),
                page.next_cursor,
                page.capabilities,
            )

        super().__init__(
            adapter_id=adapter_id,
            source="matrix",
            reader=filtered_reader,
            item_mapper=_matrix_item_mapper,
        )
