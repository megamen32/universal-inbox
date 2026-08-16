"""Route stored inbox items into NoticePlace without owning destination policy."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .contracts import ItemIdentity
from .store import SQLiteInboxStore


_REF_RE = re.compile(r"^inbox://([a-z0-9_.-]+)/(.+)$")


@dataclass(frozen=True, slots=True)
class NoticePlaceReceipt:
    event_id: str
    incident_id: str
    delivery_id: str | None


class NoticePlaceInboxSink:
    """Publish one canonical inbox item to the route selected by its Outbox token."""

    def __init__(
        self,
        store: SQLiteInboxStore,
        event_url: str,
        token: str,
        *,
        project: str,
        recipient: str,
        severity: str = "notice",
        request_timeout: float = 8,
        runner: Any = urllib.request.urlopen,
    ) -> None:
        if not event_url.strip() or not token.strip():
            raise ValueError("NoticePlace event URL and token are required")
        self._store = store
        self._event_url = event_url.rstrip("/")
        self._token = token
        self._project = project.strip()
        self._recipient = recipient.strip()
        self._severity = severity.strip().lower()
        self._request_timeout = request_timeout
        self._runner = runner

    def __call__(self, ref: str) -> NoticePlaceReceipt:
        match = _REF_RE.fullmatch(ref)
        if match is None:
            raise ValueError("unsupported inbox reference")
        identity = ItemIdentity(match.group(1), match.group(2))
        item = self._store.get(identity)
        if item is None or item.is_tombstoned:
            raise ValueError("inbox reference does not identify a deliverable item")
        payload = {
            "schema": "notify.event.v1",
            "project": self._project,
            "recipient": self._recipient,
            "kind": "notification",
            "severity": self._severity,
            "title": item.title or f"{identity.source} message",
            "body": item.body or "",
            "dedup_key": f"inbox:{identity.source}:{identity.item_id}",
            "correlation_id": ref,
            "event_type": "inbox.message",
            "producer": "universal-inbox",
            "plugin": identity.source,
        }
        idempotency_key = "inbox-" + hashlib.sha256(ref.encode()).hexdigest()
        request = urllib.request.Request(
            self._event_url,
            data=json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
                "Idempotency-Key": idempotency_key,
            },
            method="POST",
        )
        try:
            with self._runner(request, timeout=self._request_timeout) as response:
                if int(response.status) != 202:
                    raise RuntimeError(f"NoticePlace returned HTTP {response.status}")
                result = json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"NoticePlace returned HTTP {error.code}") from error
        except json.JSONDecodeError as error:
            raise RuntimeError("NoticePlace returned invalid JSON") from error
        event_id = result.get("event_id") if isinstance(result, dict) else None
        incident_id = result.get("incident_id") if isinstance(result, dict) else None
        delivery_id = result.get("initial_delivery_id") if isinstance(result, dict) else None
        if not isinstance(event_id, str) or not isinstance(incident_id, str):
            raise RuntimeError("NoticePlace returned an invalid acceptance receipt")
        return NoticePlaceReceipt(event_id, incident_id, delivery_id if isinstance(delivery_id, str) else None)


def build_routed_noticeplace_sink(
    store: SQLiteInboxStore,
    event_url: str,
    *,
    routes: dict[str, dict[str, object]],
    project: str,
    recipient: str = "operator",
    severity: str = "notice",
    request_timeout: float = 8,
    runner: Any = urllib.request.urlopen,
):
    """Select one operator-owned NoticePlace consumer route by inbox source."""
    sinks = {}
    for source, config in routes.items():
        token = str(config.get("token") or "").strip()
        if not token:
            raise ValueError(f"NoticePlace route {source} requires a token")
        sinks[source.strip().lower()] = NoticePlaceInboxSink(
            store,
            event_url,
            token,
            project=str(config.get("project") or project),
            recipient=str(config.get("recipient") or recipient),
            severity=str(config.get("severity") or severity),
            request_timeout=request_timeout,
            runner=runner,
        )

    def route(ref: str) -> NoticePlaceReceipt:
        match = _REF_RE.fullmatch(ref)
        if match is None:
            raise ValueError("unsupported inbox reference")
        sink = sinks.get(match.group(1))
        if sink is None:
            raise ValueError(f"no NoticePlace route for source {match.group(1)}")
        return sink(ref)

    return route
