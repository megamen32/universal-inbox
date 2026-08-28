"""Process entrypoint for the opt-in Overpod Telegram secretary watch."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from pathlib import Path

from .store import SQLiteInboxStore
from .telegram_runtime import build_overpod_configured_telegram_watch, run_telegram_watch


def _positive_float(value: str, name: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{name} must be a number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"{name} must be positive")
    return parsed


def _positive_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{name} must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"{name} must be positive")
    return parsed


def main(argv: list[str] | None = None) -> int:
    environment = os.environ
    parser = argparse.ArgumentParser(prog="universal-inbox-secretary")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path(environment.get("UNIVERSAL_INBOX_DB_PATH", "./universal-inbox.sqlite3")),
    )
    parser.add_argument(
        "--interval-seconds",
        type=lambda value: _positive_float(value, "interval-seconds"),
        default=_positive_float(environment.get("UNIVERSAL_INBOX_TELEGRAM_POLL_INTERVAL_SECONDS", "5"), "poll interval"),
    )
    parser.add_argument(
        "--limit",
        type=lambda value: _positive_int(value, "limit"),
        default=_positive_int(environment.get("UNIVERSAL_INBOX_TELEGRAM_POLL_LIMIT", "100"), "poll limit"),
    )
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    args = parser.parse_args(argv)

    stop_event = threading.Event()
    if not args.once:
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda _signum, _frame: stop_event.set())

    def on_error(error: Exception) -> None:
        print(f"[universal-inbox-secretary] {type(error).__name__}", file=sys.stderr, flush=True)

    with SQLiteInboxStore(args.db_path) as store:
        watch = build_overpod_configured_telegram_watch(store, environment=environment)
        if args.once:
            watch.poll_once(limit=args.limit)
        else:
            run_telegram_watch(
                watch,
                stop_event=stop_event,
                interval_seconds=args.interval_seconds,
                limit=args.limit,
                on_error=on_error,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
