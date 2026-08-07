"""Dependency-free newline-delimited JSON-RPC transport for the core facade.

The transport speaks a tiny MCP-shaped surface over stdio:

- ``initialize`` returns the server identity and the tool capability marker.
- ``tools/list`` advertises the concrete tools exposed by ``CoreMcpSurface``.
- ``tools/call`` dispatches a named tool with JSON arguments and returns the
  structured result from the core facade.

Each input line is parsed independently. Malformed JSON, missing protocol
fields, and unknown methods fail closed with a JSON-RPC error response.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from .mcp_surface import CoreMcpSurface
from .store import SQLiteInboxStore

JSONValue = dict[str, Any]


class StdioJsonRpcTransport:
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
            self._write_result(request_id, self._tools_list())
            return
        if method == "tools/call":
            self._write_result(request_id, self._tools_call(params))
            return
        self._write_error(request_id, -32601, "Method not found")

    def _initialize(self) -> dict[str, Any]:
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "universal-inbox-core", "version": "0.1.0"},
            "capabilities": {"tools": {}},
        }

    def _tools_list(self) -> dict[str, Any]:
        return self._surface.dispatch("tools/list", {})

    def _tools_call(self, params: Any) -> dict[str, Any]:
        return self._surface.dispatch("tools/call", params if isinstance(params, dict) else {})

    def _write_result(self, request_id: Any, result: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _write_error(self, request_id: Any, code: int, message: str) -> None:
        self._write({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def _write(self, payload: JSONValue) -> None:
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
        StdioJsonRpcTransport(CoreMcpSurface(store), sys.stdin, sys.stdout).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
