from __future__ import annotations

import json
import subprocess
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from universal_inbox.contracts import InboxItem, ItemIdentity
from universal_inbox.mcp_surface import CoreMcpSurface
from universal_inbox.store import SQLiteInboxStore


def _run_stdio_session(
    tmp_path: Path,
    requests: list[dict[str, object] | str],
    *,
    module: str = "universal_inbox",
) -> list[dict[str, object]]:
    db_path = tmp_path / "inbox.sqlite3"
    store = SQLiteInboxStore(db_path)
    store.ingest(
        InboxItem(
            identity=ItemIdentity("telegram", "m-1"),
            title="Orchid follow-up",
            body="A bounded preview",
        )
    )
    store.close()

    proc = subprocess.Popen(
        [sys.executable, "-m", module, "--db-path", str(db_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None

    payload = "\n".join(req if isinstance(req, str) else json.dumps(req) for req in requests) + "\n"
    stdout, stderr = proc.communicate(payload, timeout=10)
    assert proc.returncode == 0, stderr
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def test_stdio_transport_initialize_list_and_call(tmp_path: Path) -> None:
    store = SQLiteInboxStore(tmp_path / "canonical.sqlite3")
    expected_tool_names = [tool["name"] for tool in CoreMcpSurface(store).tool_manifest()["tools"]]

    responses = _run_stdio_session(
        tmp_path,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "inbox.search", "arguments": {"query": "orchid", "limit": 5}},
            },
        ],
    )

    assert [response["id"] for response in responses] == [1, 2, 3]
    assert responses[0]["result"]["serverInfo"]["name"] == "universal-inbox-core"
    tool_names = [tool["name"] for tool in responses[1]["result"]["tools"]]
    assert tool_names == expected_tool_names
    assert responses[2]["result"]["ok"] is True
    assert responses[2]["result"]["items"][0]["ref"] == "inbox://telegram/m-1"


def test_module_entrypoint_matches_canonical_manifest(tmp_path: Path) -> None:
    store = SQLiteInboxStore(tmp_path / "manifest.sqlite3")
    expected_tool_names = [tool["name"] for tool in CoreMcpSurface(store).tool_manifest()["tools"]]

    responses = _run_stdio_session(
        tmp_path,
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}],
        module="universal_inbox.transport",
    )

    assert [tool["name"] for tool in responses[0]["result"]["tools"]] == expected_tool_names


def test_module_entrypoint_preserves_tools_call_jsonrpc(tmp_path: Path) -> None:
    responses = _run_stdio_session(
        tmp_path,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "inbox.search", "arguments": {"query": "orchid"}}},
        ],
        module="universal_inbox.transport",
    )

    assert responses[0]["id"] == 1
    assert responses[0]["result"]["ok"] is True
    assert responses[0]["result"]["items"][0]["ref"] == "inbox://telegram/m-1"


def test_stdio_transport_rejects_malformed_and_unknown_requests(tmp_path: Path) -> None:
    responses = _run_stdio_session(
        tmp_path,
        [
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
            '{"jsonrpc":"2.0","id":2,"method":"does/not-exist","params":{}}',
            "{not-json}",
        ],
    )

    assert responses[0]["result"]["serverInfo"]["name"] == "universal-inbox-core"
    assert responses[1]["error"]["code"] == -32601
    assert responses[2]["error"]["code"] == -32700
