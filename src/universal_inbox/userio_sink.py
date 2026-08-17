"""One-way canonical Inbox delivery to the UserIO business control plane."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from .contracts import ItemIdentity
from .store import SQLiteInboxStore


class UserIOInboxSink:
    def __init__(
        self, store: SQLiteInboxStore, ingress_url: str, token: str, *, route_for_source: Mapping[str, str], runner: Any = urllib.request.urlopen
    ) -> None:
        if not ingress_url.startswith(("http://", "https://")) or not token.strip():
            raise ValueError("UserIO ingress URL and token are required")
        self._store = store
        self._ingress_url = ingress_url.rstrip("/")
        self._token = token
        self._route_for_source = {str(source).strip().lower(): str(route).strip() for source, route in route_for_source.items() if str(route).strip()}
        self._runner = runner

    def __call__(self, ref: str) -> str:
        source, item_id = _identity_from_ref(ref)
        route_id = self._route_for_source.get(source)
        if not route_id:
            raise ValueError(f"no UserIO route for source {source}")
        item = self._store.get(ItemIdentity(source, item_id))
        if item is None or item.is_tombstoned or not item.body:
            raise ValueError("inbox reference does not identify a UserIO message")
        payload = {
            "route_id": route_id,
            "message": {
                "schema": "universal.inbox.message.v1",
                "source": source,
                "message_id": item_id,
                "sender": item.sender or item.title or item_id,
                "body": item.body,
            },
        }
        request = urllib.request.Request(
            self._ingress_url + "/v1/messages",
            data=json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._token}"},
            method="POST",
        )
        try:
            with self._runner(request, timeout=8) as response:
                if int(response.status) != 202:
                    raise RuntimeError(f"UserIO returned HTTP {response.status}")
                result = json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"UserIO returned HTTP {error.code}") from error
        conversation_id = result.get("conversation_id") if isinstance(result, dict) else None
        if not isinstance(conversation_id, str) or not conversation_id:
            raise RuntimeError("UserIO returned invalid acceptance receipt")
        return conversation_id


class FanoutSink:
    """Both destinations must acknowledge before Inbox advances its source cursor."""

    def __init__(self, *sinks: Callable[[str], object]) -> None:
        self._sinks = sinks

    def __call__(self, ref: str) -> tuple[object, ...]:
        return tuple(sink(ref) for sink in self._sinks)


def _identity_from_ref(ref: str) -> tuple[str, str]:
    prefix = "inbox://"
    if not ref.startswith(prefix):
        raise ValueError("unsupported inbox reference")
    source, separator, item_id = ref[len(prefix) :].partition("/")
    if not separator or not source or not item_id:
        raise ValueError("unsupported inbox reference")
    return source, item_id
