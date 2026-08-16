"""Read-only bridge to the persistent ``@overpod/mcp-telegram`` daemon."""

from __future__ import annotations

import json
import re
import socket
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from ..adapter import MalformedAdapterResponse, PermanentAdapterError, TransientAdapterError
from ..contracts import Capability, InboxCursor
from ._read_only import ReadOnlyPage
from .telegram_mcp import TelegramMcpPreview


class OverpodTelegramClient(Protocol):
    def call(self, tool: str, args: dict[str, object]) -> object: ...


ConnectFn = Callable[[str, float], socket.socket]
_DEFAULT_SOCKET_PATH = Path.home() / ".mcp-telegram" / "daemon.sock"
_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_RESPONSE_BYTES = 512 * 1024
_MAX_UPDATE_SLICES = 16
_CURSOR_VERSION = 1
_READ_ONLY_TOOLS = frozenset(
    {
        "telegram-status",
        "telegram-get-chat-info",
        "telegram-get-state",
        "telegram-get-updates",
        "telegram-get-channel-updates",
    }
)


def _default_connect(path: str, timeout: float) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
    except Exception:
        sock.close()
        raise
    return sock


class OverpodTelegramIpcClient:
    """Call one allowlisted read-only Overpod tool over its Unix IPC socket.

    The inbox bridge only uses read-only tools.  Telegram credentials and the
    GramJS connection remain owned by the Overpod daemon.
    """

    def __init__(
        self,
        socket_path: str | Path = _DEFAULT_SOCKET_PATH,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
        connect_fn: ConnectFn | None = None,
    ) -> None:
        normalized_path = str(socket_path).strip()
        if not normalized_path:
            raise ValueError("Overpod socket path must not be empty")
        if timeout_seconds <= 0 or max_response_bytes < 1:
            raise ValueError("Overpod IPC bounds must be positive")
        self.socket_path = normalized_path
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self._connect_fn = connect_fn or _default_connect

    def call(self, tool: str, args: dict[str, object]) -> object:
        if not tool.strip():
            raise ValueError("Overpod tool name must not be empty")
        if tool not in _READ_ONLY_TOOLS:
            raise PermanentAdapterError("Overpod Telegram IPC bridge only permits read-only tools")
        request_id = uuid4().hex
        request = {
            "type": "tool",
            "id": request_id,
            "tool": tool,
            "args": args,
        }
        try:
            sock = self._connect_fn(self.socket_path, self.timeout_seconds)
            sock.settimeout(self.timeout_seconds)
        except (OSError, TimeoutError) as exc:
            raise TransientAdapterError("Overpod Telegram IPC socket is unavailable") from exc

        try:
            sock.sendall((json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            body = bytearray()
            while b"\n" not in body:
                chunk = sock.recv(16 * 1024)
                if not chunk:
                    raise TransientAdapterError("Overpod Telegram IPC socket closed without a response")
                body.extend(chunk)
                if len(body) > self.max_response_bytes:
                    raise TransientAdapterError("Overpod Telegram IPC response exceeded the configured limit")
            raw_line = bytes(body).split(b"\n", 1)[0]
            try:
                response = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise MalformedAdapterResponse("Overpod Telegram IPC returned invalid JSON") from exc
            if not isinstance(response, dict) or response.get("type") != "tool_response":
                raise MalformedAdapterResponse("Overpod Telegram IPC returned an invalid response envelope")
            if response.get("id") != request_id:
                raise MalformedAdapterResponse("Overpod Telegram IPC response id did not match the request")
            if response.get("error"):
                raise TransientAdapterError("Overpod Telegram tool call failed")
            if "result" not in response:
                raise MalformedAdapterResponse("Overpod Telegram IPC response had no result")
            return response["result"]
        except (OSError, TimeoutError) as exc:
            raise TransientAdapterError("Overpod Telegram IPC call failed") from exc
        finally:
            sock.close()


def _tool_text(result: object) -> str:
    if not isinstance(result, Mapping) or result.get("isError") is True:
        raise TransientAdapterError("Overpod Telegram returned a tool error")
    content = result.get("content")
    if not isinstance(content, list):
        raise MalformedAdapterResponse("Overpod Telegram tool result has no content")
    texts = [block.get("text") for block in content if isinstance(block, Mapping) and isinstance(block.get("text"), str)]
    if not texts:
        raise MalformedAdapterResponse("Overpod Telegram tool result has no text block")
    text = "\n".join(texts)
    if text.startswith("Error:"):
        raise TransientAdapterError("Overpod Telegram tool returned an error")
    return text


def _json_tool(client: OverpodTelegramClient, tool: str, args: dict[str, object]) -> dict[str, object]:
    try:
        decoded = json.loads(_tool_text(client.call(tool, args)))
    except json.JSONDecodeError as exc:
        raise MalformedAdapterResponse(f"Overpod {tool} returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise MalformedAdapterResponse(f"Overpod {tool} returned a non-object JSON value")
    return decoded


def normalize_group_chat_id(chat_id: str) -> str:
    """Use Telegram's stable supergroup form while accepting the bare id."""

    value = chat_id.strip()
    if not value:
        raise ValueError("Telegram group chat ID must not be empty")
    if re.fullmatch(r"\d+", value):
        return f"-100{value}"
    if re.fullmatch(r"-100\d+", value):
        return value
    return value


def parse_overpod_chat_kind(text: str) -> str:
    match = re.search(r"^\s*Type:\s*([A-Za-z_]+)\s*$", text, flags=re.MULTILINE)
    if match is None:
        raise ValueError("Overpod chat info did not contain a chat type")
    kind = match.group(1).strip().lower()
    if kind == "private":
        return "dm"
    if kind == "group":
        return "group"
    raise ValueError(f"unsupported Overpod chat type: {kind}")


def overpod_chat_kind_reader(client: OverpodTelegramClient) -> Callable[[str], str]:
    def read(chat_id: str) -> str:
        return parse_overpod_chat_kind(_tool_text(client.call("telegram-get-chat-info", {"chatId": chat_id})))

    return read


class OverpodTelegramIpcReader:
    """Turn Overpod's stateless global update cursor into a read-only page."""

    def __init__(
        self,
        client: OverpodTelegramClient,
        *,
        dm_chat_id: str,
        group_chat_id: str,
        own_user_id: str | None = None,
        max_slices: int = _MAX_UPDATE_SLICES,
    ) -> None:
        self._client = client
        self.dm_chat_id = dm_chat_id.strip()
        self.group_chat_id = normalize_group_chat_id(group_chat_id)
        if not self.dm_chat_id or self.dm_chat_id == self.group_chat_id:
            raise ValueError("Overpod Telegram DM and group IDs must be distinct")
        if max_slices < 1:
            raise ValueError("max_slices must be positive")
        self._own_user_id = own_user_id.strip() if own_user_id and own_user_id.strip() else None
        self._max_slices = max_slices

    def __call__(self, cursor: InboxCursor | None, limit: int) -> ReadOnlyPage[TelegramMcpPreview]:
        if limit < 1:
            raise ValueError("limit must be positive")
        if cursor is not None and cursor.source not in {None, "telegram"}:
            raise ValueError("Telegram cursor source mismatch")
        own_user_id = self._own_user_id or self._discover_own_user_id()
        state = self._decode_cursor(cursor) if cursor is not None else self._get_state()
        final_state, messages = self._fetch_updates(state, limit)
        next_cursor = self._encode_cursor(final_state)
        items: list[TelegramMcpPreview] = []
        for message in messages:
            preview = self._preview(message, own_user_id, next_cursor)
            if preview is not None:
                items.append(preview)
        return ReadOnlyPage(
            items=tuple(items),
            next_cursor=next_cursor,
            capabilities=frozenset({Capability.POLL}),
        )

    def _discover_own_user_id(self) -> str:
        text = _tool_text(self._client.call("telegram-status", {}))
        match = re.search(r"\bid:\s*(\d+)\b", text)
        if match is None:
            raise MalformedAdapterResponse("Overpod Telegram status did not expose the current user id")
        self._own_user_id = match.group(1)
        return self._own_user_id

    def _get_state(self) -> dict[str, int]:
        return self._validate_state(_json_tool(self._client, "telegram-get-state", {}))

    def _fetch_updates(self, state: dict[str, int], limit: int) -> tuple[dict[str, int], list[dict[str, object]]]:
        current = state
        messages: list[dict[str, object]] = []
        for _ in range(self._max_slices):
            payload = _json_tool(
                self._client,
                "telegram-get-updates",
                {
                    "pts": current["pts"],
                    "qts": current["qts"],
                    "date": current["date"],
                    "ptsLimit": min(limit, 1000),
                    "ptsTotalLimit": min(limit, 1000),
                },
            )
            new_messages = payload.get("newMessages")
            if not isinstance(new_messages, list) or not all(isinstance(item, dict) for item in new_messages):
                raise MalformedAdapterResponse("Overpod Telegram updates had invalid newMessages")
            messages.extend(new_messages)
            if len(messages) > limit:
                raise TransientAdapterError("Overpod Telegram returned more messages than the inbox limit")
            current = self._validate_state(payload.get("state"))
            is_final = payload.get("isFinal")
            if is_final is True:
                return current, messages
            if is_final is not False:
                raise MalformedAdapterResponse("Overpod Telegram updates had invalid isFinal")
        raise TransientAdapterError("Overpod Telegram update diff did not converge")

    def _preview(
        self,
        message: dict[str, object],
        own_user_id: str,
        cursor: str,
    ) -> TelegramMcpPreview | None:
        peer = message.get("peer")
        if not isinstance(peer, Mapping):
            raise MalformedAdapterResponse("Overpod Telegram update had no peer")
        peer_kind = peer.get("kind")
        peer_id = str(peer.get("id", "")).strip()
        chat_id: str | None = None
        if peer_kind == "user" and peer_id == self.dm_chat_id:
            chat_id = self.dm_chat_id
        elif peer_kind in {"channel", "chat"} and peer_id in {self.group_chat_id, self.group_chat_id.removeprefix("-100")}:
            chat_id = self.group_chat_id
        if chat_id is None:
            return None
        if message.get("isService") is True:
            return None
        from_id = message.get("fromId")
        if isinstance(from_id, Mapping) and from_id.get("kind") == "user" and str(from_id.get("id", "")) == own_user_id:
            return None
        message_id = str(message.get("id", "")).strip()
        if not message_id:
            raise MalformedAdapterResponse("Overpod Telegram update had no message id")
        text = message.get("text", "")
        if not isinstance(text, str):
            raise MalformedAdapterResponse("Overpod Telegram update had non-text message content")
        sender = None
        if isinstance(from_id, Mapping) and from_id.get("id") is not None:
            sender = str(from_id["id"])
        return TelegramMcpPreview(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            sender=sender,
            cursor=cursor,
        )

    @staticmethod
    def _validate_state(value: object) -> dict[str, int]:
        if not isinstance(value, Mapping):
            raise MalformedAdapterResponse("Overpod Telegram update state is not an object")
        state: dict[str, int] = {}
        for key in ("pts", "qts", "date"):
            raw = value.get(key)
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise MalformedAdapterResponse(f"Overpod Telegram state field {key} is invalid")
            state[key] = raw
        return state

    @staticmethod
    def _decode_cursor(cursor: InboxCursor) -> dict[str, int]:
        try:
            payload = json.loads(cursor.value)
        except json.JSONDecodeError as exc:
            raise MalformedAdapterResponse("Telegram cursor is not valid JSON") from exc
        if not isinstance(payload, Mapping) or payload.get("version") != _CURSOR_VERSION:
            raise MalformedAdapterResponse("Telegram cursor has an unsupported version")
        return OverpodTelegramIpcReader._validate_state(payload.get("state"))

    @staticmethod
    def _encode_cursor(state: dict[str, int]) -> str:
        return json.dumps({"version": _CURSOR_VERSION, "state": state}, sort_keys=True, separators=(",", ":"))
