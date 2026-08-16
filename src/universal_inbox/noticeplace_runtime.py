"""Opt-in Telegram Inbox to NoticePlace Outbox process."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from time import sleep

from .adapters.overpod_telegram import (
    OverpodTelegramClient,
    OverpodTelegramIpcClient,
    OverpodTelegramIpcReader,
    normalize_group_chat_id,
)
from .adapters.matrix_client import MatrixClientReader, MatrixReadAdapter
from .adapters.telegram_bot_api import TelegramBotApiReader
from .adapters.telegram_mcp import TelegramMcpReadAdapter
from .adapters._read_only import ReadOnlyPage
from .noticeplace_sink import NoticePlaceInboxSink, build_routed_noticeplace_sink
from .secretary_watch import SecretaryWatch
from .store import SQLiteInboxStore


def _noticeplace_sink(store, environment: Mapping[str, str], *, runner: Any = None):
    event_url = environment.get("UNIVERSAL_INBOX_NOTICEPLACE_EVENT_URL", "").strip()
    token = environment.get("UNIVERSAL_INBOX_NOTICEPLACE_TOKEN", "").strip()
    routes_raw = environment.get("UNIVERSAL_INBOX_NOTICEPLACE_ROUTES_JSON", "").strip()
    if not event_url:
        raise RuntimeError("UNIVERSAL_INBOX_NOTICEPLACE_EVENT_URL is required")
    sink_kwargs = {"runner": runner} if runner is not None else {}
    if routes_raw:
        try:
            routes = json.loads(routes_raw)
        except json.JSONDecodeError as error:
            raise RuntimeError("UNIVERSAL_INBOX_NOTICEPLACE_ROUTES_JSON must be a JSON object") from error
        if not isinstance(routes, dict) or not routes:
            raise RuntimeError("UNIVERSAL_INBOX_NOTICEPLACE_ROUTES_JSON must be a non-empty JSON object")
        return build_routed_noticeplace_sink(
            store,
            event_url,
            routes=routes,
            project=environment.get("UNIVERSAL_INBOX_NOTICEPLACE_PROJECT", "universal-inbox"),
            recipient=environment.get("UNIVERSAL_INBOX_NOTICEPLACE_RECIPIENT", "operator"),
            severity=environment.get("UNIVERSAL_INBOX_NOTICEPLACE_SEVERITY", "notice"),
            request_timeout=float(environment.get("UNIVERSAL_INBOX_NOTICEPLACE_TIMEOUT_SECONDS", "8")),
            **sink_kwargs,
        )
    if not token:
        raise RuntimeError("UNIVERSAL_INBOX_NOTICEPLACE_TOKEN is required")
    return NoticePlaceInboxSink(
        store,
        event_url,
        token,
        project=environment.get("UNIVERSAL_INBOX_NOTICEPLACE_PROJECT", "universal-inbox"),
        recipient=environment.get("UNIVERSAL_INBOX_NOTICEPLACE_RECIPIENT", "operator"),
        severity=environment.get("UNIVERSAL_INBOX_NOTICEPLACE_SEVERITY", "notice"),
        request_timeout=float(environment.get("UNIVERSAL_INBOX_NOTICEPLACE_TIMEOUT_SECONDS", "8")),
        **sink_kwargs,
    )


def build_noticeplace_telegram_watch(
    store: SQLiteInboxStore,
    *,
    environment: Mapping[str, str] | None = None,
    ipc_client: OverpodTelegramClient | None = None,
    telegram_reader: Any = None,
    http_runner: Any = None,
) -> SecretaryWatch:
    """Compose the existing durable Inbox watcher with route-neutral Outbox intake."""
    environment = environment or os.environ
    sink = _noticeplace_sink(store, environment, runner=http_runner)
    dm_id = environment.get("UNIVERSAL_INBOX_TELEGRAM_DM_CHAT_ID", "").strip()
    group_id = normalize_group_chat_id(environment.get("UNIVERSAL_INBOX_TELEGRAM_GROUP_CHAT_ID", ""))
    if not dm_id or not group_id or dm_id == group_id:
        raise RuntimeError("explicit distinct Telegram DM and group chat IDs are required")
    ignored_senders = frozenset(
        part.strip()
        for part in environment.get("UNIVERSAL_INBOX_TELEGRAM_IGNORED_SENDER_IDS", "").split(",")
        if part.strip()
    )
    transport = environment.get("UNIVERSAL_INBOX_TELEGRAM_TRANSPORT", "overpod").strip().lower()
    if telegram_reader is not None:
        reader = telegram_reader
    elif transport == "bot-api":
        if environment.get("UNIVERSAL_INBOX_TELEGRAM_SINGLE_READER", "").strip().lower() not in {"1", "true", "yes"}:
            raise RuntimeError("Bot API ingress requires explicit single-reader ownership")
        token = environment.get("UNIVERSAL_INBOX_TELEGRAM_BOT_TOKEN", "").strip()
        bot_id = environment.get("UNIVERSAL_INBOX_TELEGRAM_ACCOUNT_ID", "").strip()
        if not token or not bot_id:
            raise RuntimeError("Telegram Bot API ingress requires bot token and bot account id")
        reader = TelegramBotApiReader(
            token,
            allowed_chat_ids=(dm_id, group_id),
            own_bot_id=bot_id,
            ignored_sender_ids=ignored_senders,
            timeout_seconds=float(environment.get("UNIVERSAL_INBOX_TELEGRAM_BOT_TIMEOUT_SECONDS", "10")),
        )
    elif transport == "overpod":
        socket_path = environment.get("UNIVERSAL_INBOX_OVERPOD_SOCKET_PATH", "").strip()
        client = ipc_client or (
            OverpodTelegramIpcClient(
                socket_path,
                timeout_seconds=float(environment.get("UNIVERSAL_INBOX_OVERPOD_IPC_TIMEOUT_SECONDS", "30")),
            )
            if socket_path
            else OverpodTelegramIpcClient(
                timeout_seconds=float(environment.get("UNIVERSAL_INBOX_OVERPOD_IPC_TIMEOUT_SECONDS", "30")),
            )
        )
        reader = OverpodTelegramIpcReader(
            client,
            dm_chat_id=dm_id,
            group_chat_id=group_id,
            own_user_id=environment.get("UNIVERSAL_INBOX_TELEGRAM_ACCOUNT_ID"),
        )
    else:
        raise RuntimeError("UNIVERSAL_INBOX_TELEGRAM_TRANSPORT must be overpod or bot-api")

    def loop_safe_reader(cursor, limit):
        page = reader(cursor, limit)
        return ReadOnlyPage(
            tuple(item for item in page.items if str(item.sender or "") not in ignored_senders),
            page.next_cursor,
            page.capabilities,
        )
    adapter_id = "telegram-bot-api-noticeplace" if transport == "bot-api" else "telegram-overpod-noticeplace"
    return SecretaryWatch(
        store,
        TelegramMcpReadAdapter(
            adapter_id=adapter_id,
            reader=loop_safe_reader,
            allowed_chat_ids=(dm_id, group_id),
        ),
        sink,
        wake_lease_seconds=float(environment.get("UNIVERSAL_INBOX_NOTICEPLACE_LEASE_SECONDS", "120")),
    )


def build_noticeplace_matrix_watch(
    store: SQLiteInboxStore,
    *,
    environment: Mapping[str, str] | None = None,
    matrix_runner: Any = None,
    http_runner: Any = None,
) -> SecretaryWatch:
    """Compose allowlisted Matrix `/sync` ingress with the same Outbox sink."""
    environment = environment or os.environ
    sink = _noticeplace_sink(store, environment, runner=http_runner)
    homeserver = environment.get("UNIVERSAL_INBOX_MATRIX_HOMESERVER", "").strip()
    matrix_token = environment.get("UNIVERSAL_INBOX_MATRIX_ACCESS_TOKEN", "").strip()
    own_user_id = environment.get("UNIVERSAL_INBOX_MATRIX_USER_ID", "").strip()
    room_ids = tuple(part.strip() for part in environment.get("UNIVERSAL_INBOX_MATRIX_ROOM_IDS", "").split(",") if part.strip())
    if not homeserver or not matrix_token or not own_user_id or not room_ids:
        raise RuntimeError("Matrix ingress requires homeserver, access token, user id, and room allowlist")
    reader_kwargs = {}
    if matrix_runner is not None:
        reader_kwargs["runner"] = matrix_runner
    reader = MatrixClientReader(
        homeserver,
        matrix_token,
        allowed_room_ids=room_ids,
        own_user_id=own_user_id,
        timeout_seconds=float(environment.get("UNIVERSAL_INBOX_MATRIX_TIMEOUT_SECONDS", "30")),
        **reader_kwargs,
    )
    return SecretaryWatch(
        store,
        MatrixReadAdapter(adapter_id="matrix-client-noticeplace", reader=reader, allowed_room_ids=room_ids),
        sink,
        wake_lease_seconds=float(environment.get("UNIVERSAL_INBOX_NOTICEPLACE_LEASE_SECONDS", "120")),
    )


def run_noticeplace_watch(
    watch: SecretaryWatch,
    *,
    stop_event: threading.Event | None = None,
    interval_seconds: float = 5,
    limit: int = 100,
    on_error: Any = None,
) -> None:
    """Continuously poll one configured source until service shutdown."""
    if interval_seconds <= 0 or limit < 1:
        raise ValueError("poll interval and limit must be positive")
    while stop_event is None or not stop_event.is_set():
        try:
            watch.poll_once(limit=limit)
        except Exception as error:
            if on_error is None:
                raise
            on_error(error)
        if stop_event is None:
            sleep(interval_seconds)
        else:
            stop_event.wait(interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m universal_inbox.noticeplace_runtime")
    parser.add_argument("--source", choices=("telegram", "matrix"), default=os.environ.get("UNIVERSAL_INBOX_SOURCE", "telegram"))
    parser.add_argument("--db-path", type=Path, default=Path(os.environ.get("UNIVERSAL_INBOX_DB_PATH", "./universal-inbox.sqlite3")))
    parser.add_argument("--limit", type=int, default=int(os.environ.get("UNIVERSAL_INBOX_TELEGRAM_POLL_LIMIT", "100")))
    parser.add_argument("--interval-seconds", type=float, default=float(os.environ.get("UNIVERSAL_INBOX_POLL_INTERVAL_SECONDS", "5")))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.limit < 1 or args.interval_seconds <= 0:
        parser.error("--limit and --interval-seconds must be positive")
    stop_event = threading.Event()
    if not args.once:
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda _signum, _frame: stop_event.set())

    def on_error(error: Exception) -> None:
        print(f"[universal-inbox-noticeplace] {type(error).__name__}", file=sys.stderr, flush=True)

    with SQLiteInboxStore(args.db_path) as store:
        builder = build_noticeplace_telegram_watch if args.source == "telegram" else build_noticeplace_matrix_watch
        watch = builder(store)
        if args.once:
            result = watch.poll_once(limit=args.limit)
            print(f"accepted={len(result.emitted_refs)}")
        else:
            run_noticeplace_watch(
                watch,
                stop_event=stop_event,
                interval_seconds=args.interval_seconds,
                limit=args.limit,
                on_error=on_error,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
