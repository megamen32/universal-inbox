from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, TextIO

from .mcp_surface import CoreMcpSurface
from .adapters.gmail import GmailHimalayaReader, GmailReadAdapter
from .registry import AdapterRegistry
from .store import SQLiteInboxStore


class _StdioJsonRpcTransport:
    def __init__(self, surface: CoreMcpSurface, input_stream: TextIO, output_stream: TextIO) -> None:
        self._surface = surface
        self._input_stream = input_stream
        self._output_stream = output_stream

    def serve(self) -> None:
        for line in self._input_stream:
            text = line.strip()
            if not text:
                continue
            self._handle_raw_line(text)

    def _handle_raw_line(self, text: str) -> None:
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            self._write_error(None, -32700, "Parse error")
            return
        if not isinstance(message, dict):
            self._write_error(None, -32600, "Invalid Request")
            return
        if message.get("jsonrpc") != "2.0":
            self._write_error(message.get("id"), -32600, "Invalid Request")
            return

        method = message.get("method")
        if not isinstance(method, str) or not method:
            self._write_error(message.get("id"), -32600, "Invalid Request")
            return

        request_id = message.get("id")
        if request_id is None:
            return

        params = message.get("params", {})
        if method == "initialize":
            self._write_result(request_id, self._initialize())
            return
        if method == "tools/list":
            self._write_result(request_id, self._surface.dispatch("tools/list", {}))
            return
        if method == "tools/call":
            self._write_result(request_id, self._surface.dispatch("tools/call", params))
            return
        self._write_error(request_id, -32601, "Method not found")

    @staticmethod
    def _initialize() -> dict[str, Any]:
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "universal-inbox-core", "version": "0.1.0"},
            "capabilities": {"tools": {}},
        }

    def _write_result(self, request_id: Any, result: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _write_error(self, request_id: Any, code: int, message: str) -> None:
        self._write({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def _write(self, payload: dict[str, Any]) -> None:
        json.dump(payload, self._output_stream, ensure_ascii=False, separators=(",", ":"))
        self._output_stream.write("\n")
        self._output_stream.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m universal_inbox", add_help=True)
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path.cwd() / "universal-inbox.sqlite3",
        help="SQLite path for the local inbox store.",
    )
    args = parser.parse_args(argv)

    with SQLiteInboxStore(args.db_path) as store:
        _StdioJsonRpcTransport(
            CoreMcpSurface(store, registry=_default_registry()),
            sys.stdin,
            sys.stdout,
        ).serve()
    return 0


def _default_registry() -> AdapterRegistry:
    """Register only the local, already-configured read-only Gmail seam."""

    binary = os.getenv("UNIVERSAL_INBOX_HIMALAYA_BIN", "himalaya").strip()
    if not binary or shutil.which(binary) is None:
        return AdapterRegistry()
    account = os.getenv("UNIVERSAL_INBOX_GMAIL_ACCOUNT", "gmail").strip()
    mailbox = os.getenv("UNIVERSAL_INBOX_GMAIL_MAILBOX", "Inbox").strip()
    if account not in {"gmail", "careviolan"} or mailbox not in {"Inbox", "[Gmail]/Спам"}:
        return AdapterRegistry()
    return AdapterRegistry(
        [
            GmailReadAdapter(
                adapter_id=f"gmail-{account}-{mailbox.lower().replace('/', '-')}",
                reader=GmailHimalayaReader(
                    binary=binary,
                    account=account,
                    mailbox=mailbox,
                ),
            )
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
