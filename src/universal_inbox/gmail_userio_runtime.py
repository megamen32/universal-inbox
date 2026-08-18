"""Direct, read-only Gmail to UserIO process; no Hermes or Outbox dependency."""

from __future__ import annotations

import argparse
import os
import signal
import threading
from pathlib import Path

from .__main__ import _default_registry
from .secretary_watch import SecretaryWatch
from .store import SQLiteInboxStore
from .userio_sink import UserIOInboxSink


def build_gmail_userio_watches(store: SQLiteInboxStore, environment: dict[str, str] | None = None) -> tuple[SecretaryWatch, ...]:
    environment = dict(os.environ if environment is None else environment)
    ingress_url = environment.get("UNIVERSAL_USERIO_INGRESS_URL", "http://127.0.0.1:18093").strip()
    token = environment.get("UNIVERSAL_USERIO_INGRESS_TOKEN", environment.get("USERIO_API_TOKEN", "")).strip()
    if not token:
        raise RuntimeError("UNIVERSAL_USERIO_INGRESS_TOKEN or USERIO_API_TOKEN is required")
    watches: list[SecretaryWatch] = []
    for adapter in _default_registry().adapters():
        if not adapter.manifest.source.startswith("gmail"):
            continue
        source = adapter.manifest.source
        sink = UserIOInboxSink(store, ingress_url, token, route_for_source={source: "gmail-read-only"})
        watches.append(SecretaryWatch(store, adapter, sink))
    if not watches:
        raise RuntimeError("no configured Gmail adapters")
    return tuple(watches)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="universal-inbox-gmail-userio")
    parser.add_argument("--db-path", type=Path, default=Path(os.getenv("UNIVERSAL_INBOX_DB_PATH", "/var/lib/universal-inbox/gmail.sqlite3")))
    parser.add_argument("--interval-seconds", type=float, default=float(os.getenv("UNIVERSAL_INBOX_GMAIL_POLL_INTERVAL_SECONDS", "60")))
    parser.add_argument("--limit", type=int, default=int(os.getenv("UNIVERSAL_INBOX_GMAIL_POLL_LIMIT", "100")))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.interval_seconds <= 0 or args.limit < 1:
        raise SystemExit("poll interval and limit must be positive")
    args.db_path.parent.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    if not args.once:
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda _signum, _frame: stop.set())
    with SQLiteInboxStore(args.db_path) as store:
        watches = build_gmail_userio_watches(store)
        while not stop.is_set():
            for watch in watches:
                watch.poll_once(limit=args.limit)
            if args.once:
                break
            stop.wait(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
