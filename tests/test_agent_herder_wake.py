from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.request import Request

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from universal_inbox.agent_herder_wake import AgentHerderMcpWakeSink, AgentHerderWakeError, HttpResponse


def test_sink_initializes_mcp_and_dispatches_only_opaque_browser_wake_fields() -> None:
    calls: list[tuple[dict[str, object], Request]] = []

    def post(request: Request, _timeout: float) -> HttpResponse:
        payload = json.loads(request.data or b"{}")
        calls.append((payload, request))
        method = payload.get("method")
        if method == "initialize":
            return HttpResponse(200, {"mcp-session-id": "session-1"}, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode())
        if method == "notifications/initialized":
            return HttpResponse(202, {"mcp-session-id": "session-1"}, b"")
        assert method == "tools/call"
        arguments = payload["params"]["arguments"]
        now = "2026-08-10T00:00:00.000Z"
        worker_receipt = {
            "worker": "mac-mini-browserclaw",
            "target": "E-Frontier",
            "templateId": "secretary.inbox.v1",
            "runId": arguments["runId"],
            "idempotencyId": arguments["idempotencyId"],
            "receiptRef": f"receipt:{arguments['idempotencyId']}",
            "status": "completed",
            "acceptedAt": now,
            "completedAt": now,
        }
        record = {
            "request": arguments,
            "status": "completed",
            "attempts": 1,
            "requestedAt": now,
            "updatedAt": now,
            "receipt": worker_receipt,
        }
        return HttpResponse(
            200,
            {},
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": json.dumps(record)}]}}).encode(),
        )

    sink = AgentHerderMcpWakeSink(
        "http://127.0.0.1:18787/mcp",
        token="test-token",
        http_post=post,
    )
    sink("inbox://telegram/chat:42")
    sink("inbox://telegram/chat:43")

    assert [call[0]["method"] for call in calls] == ["initialize", "notifications/initialized", "tools/call", "tools/call"]
    initialize, notification = (call[1] for call in calls[:2])
    assert initialize.get_header("Authorization") == "Bearer test-token"
    assert notification.get_header("Mcp-session-id") == "session-1"
    arguments = calls[2][0]["params"]["arguments"]
    assert arguments["target"] == "E-Frontier"
    assert arguments["templateId"] == "secretary.inbox.v1"
    assert arguments["sourceRefs"] == ["inbox://telegram/chat:42"]
    assert "secret-text" not in json.dumps(arguments)
    assert set(arguments) == {
        "schema", "worker", "target", "templateId", "sourceRefs", "runId", "idempotencyId", "deadlineMs",
    }


def test_sink_raises_on_failed_worker_receipt_so_outbox_can_retry() -> None:
    def post(request: Request, _timeout: float) -> HttpResponse:
        payload = json.loads(request.data or b"{}")
        if payload.get("method") == "initialize":
            return HttpResponse(200, {"mcp-session-id": "session-1"}, json.dumps({"result": {}}).encode())
        if payload.get("method") == "notifications/initialized":
            return HttpResponse(202, {"mcp-session-id": "session-1"}, b"")
        record = {
            "request": payload["params"]["arguments"],
            "status": "failed",
            "attempts": 1,
            "requestedAt": "2026-08-10T00:00:00.000Z",
            "updatedAt": "2026-08-10T00:00:00.000Z",
            "errorClass": "worker_unavailable",
        }
        return HttpResponse(200, {"mcp-session-id": "session-1"}, json.dumps({"result": {"content": [{"text": json.dumps(record)}]}}).encode())

    sink = AgentHerderMcpWakeSink("http://127.0.0.1:18787/mcp", http_post=post)
    with pytest.raises(AgentHerderWakeError, match="failed"):
        sink("inbox://telegram/chat:43")


def test_sink_rejects_unbounded_or_non_http_endpoint() -> None:
    with pytest.raises(ValueError, match="http"):
        AgentHerderMcpWakeSink("file:///tmp/herder")
    with pytest.raises(ValueError, match="loopback"):
        AgentHerderMcpWakeSink("http://agent-herder.example/mcp", token="not-enforced-server-side")
    sink = AgentHerderMcpWakeSink("http://127.0.0.1:18787/mcp", http_post=lambda _request, _timeout: HttpResponse(200, {}, b"{}"))
    with pytest.raises(ValueError, match="bounded"):
        sink("\n")


def test_sink_rejects_completed_record_with_failed_nested_receipt() -> None:
    def post(request: Request, _timeout: float) -> HttpResponse:
        payload = json.loads(request.data or b"{}")
        if payload.get("method") == "initialize":
            return HttpResponse(200, {"mcp-session-id": "session-1"}, json.dumps({"result": {}}).encode())
        if payload.get("method") == "notifications/initialized":
            return HttpResponse(202, {"mcp-session-id": "session-1"}, b"")
        arguments = payload["params"]["arguments"]
        now = "2026-08-10T00:00:00.000Z"
        receipt = {
            "worker": "mac-mini-browserclaw",
            "target": "E-Frontier",
            "templateId": "secretary.inbox.v1",
            "runId": arguments["runId"],
            "idempotencyId": arguments["idempotencyId"],
            "receiptRef": f"receipt:{arguments['idempotencyId']}",
            "status": "failed",
            "acceptedAt": now,
            "failedAt": now,
            "errorClass": "browser_action_failed",
        }
        record = {
            "request": arguments,
            "status": "completed",
            "attempts": 1,
            "requestedAt": now,
            "updatedAt": now,
            "receipt": receipt,
        }
        return HttpResponse(200, {}, json.dumps({"result": {"content": [{"text": json.dumps(record)}]}}).encode())

    sink = AgentHerderMcpWakeSink("http://127.0.0.1:18787/mcp", http_post=post)
    with pytest.raises(AgentHerderWakeError, match="status mismatch"):
        sink("inbox://telegram/chat:44")
