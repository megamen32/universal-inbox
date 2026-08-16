"""Process entrypoint for WhatsApp, VK, and phone message bridges."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from http.server import ThreadingHTTPServer
from pathlib import Path

from .noticeplace_sink import NoticePlaceInboxSink, build_routed_noticeplace_sink
from .webhook_ingress import build_webhook_handler


def build_configured_webhook_handler(
    db_path: str | Path,
    *,
    environment: Mapping[str, str] | None = None,
):
    environment = environment or os.environ
    ingress_token = environment.get("UNIVERSAL_INBOX_WEBHOOK_TOKEN", "").strip()
    event_url = environment.get("UNIVERSAL_INBOX_NOTICEPLACE_EVENT_URL", "").strip()
    outbox_token = environment.get("UNIVERSAL_INBOX_NOTICEPLACE_TOKEN", "").strip()
    routes_raw = environment.get("UNIVERSAL_INBOX_NOTICEPLACE_ROUTES_JSON", "").strip()
    ignored_raw = environment.get("UNIVERSAL_INBOX_WEBHOOK_IGNORED_SENDERS_JSON", "").strip()
    if not ingress_token:
        raise RuntimeError("UNIVERSAL_INBOX_WEBHOOK_TOKEN is required")
    if not event_url:
        raise RuntimeError("UNIVERSAL_INBOX_NOTICEPLACE_EVENT_URL is required")
    if not outbox_token and not routes_raw:
        raise RuntimeError("UNIVERSAL_INBOX_NOTICEPLACE_TOKEN is required")

    routes = None
    if routes_raw:
        try:
            routes = json.loads(routes_raw)
        except json.JSONDecodeError as error:
            raise RuntimeError("UNIVERSAL_INBOX_NOTICEPLACE_ROUTES_JSON must be a JSON object") from error
        if not isinstance(routes, dict) or not routes:
            raise RuntimeError("UNIVERSAL_INBOX_NOTICEPLACE_ROUTES_JSON must be a non-empty JSON object")
    ignored_senders = {}
    if ignored_raw:
        try:
            ignored_senders = json.loads(ignored_raw)
        except json.JSONDecodeError as error:
            raise RuntimeError("UNIVERSAL_INBOX_WEBHOOK_IGNORED_SENDERS_JSON must be a JSON object") from error
        if not isinstance(ignored_senders, dict) or any(not isinstance(value, list) for value in ignored_senders.values()):
            raise RuntimeError("UNIVERSAL_INBOX_WEBHOOK_IGNORED_SENDERS_JSON must map sources to arrays")

    def sink_factory(store):
        if routes is not None:
            return build_routed_noticeplace_sink(
                store,
                event_url,
                routes=routes,
                project=environment.get("UNIVERSAL_INBOX_NOTICEPLACE_PROJECT", "universal-inbox"),
                recipient=environment.get("UNIVERSAL_INBOX_NOTICEPLACE_RECIPIENT", "operator"),
                severity=environment.get("UNIVERSAL_INBOX_NOTICEPLACE_SEVERITY", "notice"),
                request_timeout=float(environment.get("UNIVERSAL_INBOX_NOTICEPLACE_TIMEOUT_SECONDS", "8")),
            )
        return NoticePlaceInboxSink(
            store,
            event_url,
            outbox_token,
            project=environment.get("UNIVERSAL_INBOX_NOTICEPLACE_PROJECT", "universal-inbox"),
            recipient=environment.get("UNIVERSAL_INBOX_NOTICEPLACE_RECIPIENT", "operator"),
            severity=environment.get("UNIVERSAL_INBOX_NOTICEPLACE_SEVERITY", "notice"),
            request_timeout=float(environment.get("UNIVERSAL_INBOX_NOTICEPLACE_TIMEOUT_SECONDS", "8")),
        )

    return build_webhook_handler(db_path, ingress_token, sink_factory, ignored_senders=ignored_senders)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m universal_inbox.webhook_runtime")
    parser.add_argument("--db-path", type=Path, default=Path(os.environ.get("UNIVERSAL_INBOX_DB_PATH", "./universal-inbox.sqlite3")))
    parser.add_argument("--host", default=os.environ.get("UNIVERSAL_INBOX_WEBHOOK_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("UNIVERSAL_INBOX_WEBHOOK_PORT", "18092")))
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(
        (args.host, args.port),
        build_configured_webhook_handler(args.db_path),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
