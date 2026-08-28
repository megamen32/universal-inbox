"""Provider-neutral construction seam for an injected Telegram MCP reader."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from time import sleep
from typing import Literal

from .agent_herder_wake import AgentHerderMcpWakeSink, HttpPost
from .adapters.overpod_telegram import (
    OverpodTelegramClient,
    OverpodTelegramIpcClient,
    OverpodTelegramIpcReader,
    normalize_group_chat_id,
    overpod_chat_kind_reader,
)
from .adapters.telegram_mcp import TelegramMcpReadAdapter
from .secretary_watch import SecretaryWatch, WakeSink
from .store import SQLiteInboxStore


TelegramChatKind = Literal["dm", "group"]
TelegramChatKindReader = Callable[[str], TelegramChatKind | str]


def build_telegram_watch(
    store: SQLiteInboxStore,
    reader,
    wake_sink: WakeSink,
    *,
    adapter_id: str = "telegram-mcp",
    allowed_chat_ids=None,
    wake_lease_seconds: float = 120.0,
) -> SecretaryWatch:
    """Build an opt-in watcher; credentials and provider transport stay injected."""
    return SecretaryWatch(
        store,
        TelegramMcpReadAdapter(adapter_id=adapter_id, reader=reader, allowed_chat_ids=allowed_chat_ids),
        wake_sink,
        wake_lease_seconds=wake_lease_seconds,
    )


def build_configured_telegram_watch(
    store: SQLiteInboxStore,
    reader,
    *,
    environment: Mapping[str, str] | None = None,
    adapter_id: str = "telegram-mcp",
    http_post: HttpPost | None = None,
    chat_kind_reader: TelegramChatKindReader | None = None,
) -> SecretaryWatch:
    """Build the opt-in Telegram-to-Agent-Herder watch from explicit env config."""
    environment = environment or os.environ
    endpoint = environment.get("UNIVERSAL_INBOX_AGENT_HERDER_MCP_URL", "").strip()
    if not endpoint:
        raise RuntimeError("UNIVERSAL_INBOX_AGENT_HERDER_MCP_URL is required for the configured Telegram watch")
    dm_id = environment.get("UNIVERSAL_INBOX_TELEGRAM_DM_CHAT_ID", "").strip()
    raw_group_id = environment.get("UNIVERSAL_INBOX_TELEGRAM_GROUP_CHAT_ID", "").strip()
    if not raw_group_id:
        raise RuntimeError("explicit distinct Telegram DM and group chat IDs are required")
    group_id = normalize_group_chat_id(raw_group_id)
    if not dm_id or not group_id or dm_id == group_id:
        raise RuntimeError("explicit distinct Telegram DM and group chat IDs are required")
    if chat_kind_reader is None:
        raise RuntimeError("Telegram DM/group chat-kind preflight is required")
    if str(chat_kind_reader(dm_id)).strip().lower() != "dm":
        raise RuntimeError("configured Telegram DM chat ID did not pass the DM preflight")
    if str(chat_kind_reader(group_id)).strip().lower() != "group":
        raise RuntimeError("configured Telegram group chat ID did not pass the group preflight")
    chat_ids = (dm_id, group_id)
    wake_sink = AgentHerderMcpWakeSink(
        endpoint,
        token=environment.get("UNIVERSAL_INBOX_AGENT_HERDER_MCP_TOKEN"),
        deadline_ms=int(environment.get("UNIVERSAL_INBOX_AGENT_HERDER_WAKE_DEADLINE_MS", "30000")),
        http_post=http_post,
    )
    return build_telegram_watch(store, reader, wake_sink, adapter_id=adapter_id, allowed_chat_ids=chat_ids)


def build_overpod_configured_telegram_watch(
    store: SQLiteInboxStore,
    *,
    environment: Mapping[str, str] | None = None,
    adapter_id: str = "telegram-overpod-daemon",
    http_post: HttpPost | None = None,
    ipc_client: OverpodTelegramClient | None = None,
) -> SecretaryWatch:
    """Build the configured watch against the persistent Overpod Telegram daemon."""

    environment = dict(environment or os.environ)
    socket_path = environment.get("UNIVERSAL_INBOX_OVERPOD_SOCKET_PATH", "").strip()
    timeout_seconds = float(environment.get("UNIVERSAL_INBOX_OVERPOD_IPC_TIMEOUT_SECONDS", "30"))
    if ipc_client is not None:
        client = ipc_client
    elif socket_path:
        client = OverpodTelegramIpcClient(socket_path, timeout_seconds=timeout_seconds)
    else:
        client = OverpodTelegramIpcClient(timeout_seconds=timeout_seconds)
    dm_id = environment.get("UNIVERSAL_INBOX_TELEGRAM_DM_CHAT_ID", "").strip()
    raw_group_id = environment.get("UNIVERSAL_INBOX_TELEGRAM_GROUP_CHAT_ID", "").strip()
    if not raw_group_id:
        raise RuntimeError("explicit distinct Telegram DM and group chat IDs are required")
    group_id = normalize_group_chat_id(raw_group_id)
    reader = OverpodTelegramIpcReader(
        client,
        dm_chat_id=dm_id,
        group_chat_id=group_id,
        own_user_id=environment.get("UNIVERSAL_INBOX_TELEGRAM_ACCOUNT_ID"),
    )
    environment["UNIVERSAL_INBOX_TELEGRAM_GROUP_CHAT_ID"] = reader.group_chat_id
    return build_configured_telegram_watch(
        store,
        reader,
        environment=environment,
        adapter_id=adapter_id,
        http_post=http_post,
        chat_kind_reader=overpod_chat_kind_reader(client),
    )


def run_telegram_watch(
    watch: SecretaryWatch,
    *,
    stop_event: threading.Event | None = None,
    interval_seconds: float = 30.0,
    limit: int = 100,
    on_error: Callable[[Exception], None] | None = None,
) -> None:
    """Poll an already-configured watch until its owner requests shutdown."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
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
