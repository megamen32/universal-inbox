"""Authenticated HTTP ingress for provider bridges that emit message envelopes."""

from __future__ import annotations

import json
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from .contracts import InboxItem, ItemIdentity
from .store import ItemConflictError, SQLiteInboxStore


_ALLOWED_SOURCES = frozenset({"telegram", "matrix", "whatsapp", "vk", "phone"})


def build_webhook_handler(
    db_path: str | Path,
    token: str,
    sink_factory: Any,
    *,
    ignored_senders: dict[str, set[str] | frozenset[str]] | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Build one bounded bearer-authenticated message ingress handler."""
    if not token:
        raise ValueError("webhook ingress token is required")
    ignored_senders = {
        source.strip().lower(): frozenset(str(sender).strip() for sender in senders if str(sender).strip())
        for source, senders in (ignored_senders or {}).items()
    }

    class WebhookHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/inbox/messages":
                self._reply(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            authorization = self.headers.get("Authorization") or ""
            if not secrets.compare_digest(authorization, f"Bearer {token}"):
                self._reply(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
                if not 1 <= length <= 128_000:
                    raise ValueError("invalid body length")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict) or payload.get("schema") != "universal.inbox.message.v1":
                    raise ValueError("unsupported message schema")
                source = str(payload.get("source") or "").strip().lower()
                message_id = str(payload.get("message_id") or "").strip()
                body = payload.get("body")
                sender = str(payload.get("sender") or source).strip()
                if source not in _ALLOWED_SOURCES or not message_id or not isinstance(body, str):
                    raise ValueError("invalid message envelope")
                if sender in ignored_senders.get(source, frozenset()):
                    self._reply(HTTPStatus.ACCEPTED, {
                        "source": source,
                        "message_id": message_id,
                        "ignored": True,
                    })
                    return
                identity = ItemIdentity(source, message_id)
                with SQLiteInboxStore(db_path) as request_store:
                    request_store.ingest(InboxItem(identity, title=sender or source, body=body))
                    receipt = sink_factory(request_store)(f"inbox://{source}/{message_id}")
                self._reply(HTTPStatus.ACCEPTED, {
                    "source": source,
                    "message_id": message_id,
                    "event_id": getattr(receipt, "event_id", None),
                    "incident_id": getattr(receipt, "incident_id", None),
                    "delivery_id": getattr(receipt, "delivery_id", None),
                })
            except (ValueError, json.JSONDecodeError, ItemConflictError) as error:
                self._reply(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except Exception as error:
                self._reply(HTTPStatus.BAD_GATEWAY, {"error": type(error).__name__})

        def _reply(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return WebhookHandler
