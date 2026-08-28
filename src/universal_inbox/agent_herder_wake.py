"""Opt-in MCP bridge from an inbox wake ref to Agent Herder."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from ipaddress import ip_address
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class AgentHerderWakeError(RuntimeError):
    """Raised when the opaque BrowserWorker wake cannot be accepted."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


HttpPost = Callable[[Request, float], HttpResponse]
_MAX_HTTP_RESPONSE_BYTES = 64 * 1024
_OPAQUE_REF_RE = re.compile(r"[A-Za-z0-9._:/=-]+")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z")
_ERROR_CLASSES = frozenset({
    "worker_unavailable",
    "worker_timeout",
    "worker_rejected",
    "browser_session_not_found",
    "browser_action_failed",
    "invalid_receipt",
    "receipt_mismatch",
})


def _default_http_post(request: Request, timeout: float) -> HttpResponse:
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
            if len(body) > _MAX_HTTP_RESPONSE_BYTES:
                raise AgentHerderWakeError("Agent Herder response is too large")
            return HttpResponse(
                status=int(response.status),
                headers={key.lower(): value for key, value in response.headers.items()},
                body=body,
            )
    except HTTPError as error:
        raise AgentHerderWakeError(f"Agent Herder HTTP returned {error.code}") from error
    except (OSError, URLError, TimeoutError) as error:
        raise AgentHerderWakeError("Agent Herder HTTP request failed") from error


class AgentHerderMcpWakeSink:
    """Dispatch one opaque source ref through Agent Herder's MCP HTTP endpoint.

    This class deliberately owns neither Telegram credentials nor browser state.
    It reuses one MCP session for the lifetime of the sink and accepts only
    the bounded ``browser_wake`` record returned by Agent Herder.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        token: str | None = None,
        deadline_ms: int = 30_000,
        http_post: HttpPost | None = None,
    ) -> None:
        normalized_endpoint = endpoint.strip()
        parsed_endpoint = urlsplit(normalized_endpoint)
        if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.hostname:
            raise ValueError("Agent Herder MCP endpoint must use http or https")
        hostname = parsed_endpoint.hostname.lower()
        try:
            loopback = ip_address(hostname).is_loopback
        except ValueError:
            loopback = hostname == "localhost"
        if not loopback:
            raise ValueError("Agent Herder MCP endpoint must use a loopback host")
        if not 1 <= deadline_ms <= 10 * 60 * 1000:
            raise ValueError("deadline_ms must be between 1 and 600000")
        self._endpoint = normalized_endpoint
        self._token = token.strip() if token and token.strip() else None
        self._deadline_ms = deadline_ms
        self._http_post = http_post or _default_http_post
        self._lock = threading.Lock()
        self._session_id: str | None = None

    def __call__(self, source_ref: str) -> None:
        source_ref = self._validate_source_ref(source_ref)
        digest = sha256(source_ref.encode("utf-8")).hexdigest()[:32]
        request = {
            "schema": "agent-herder.browser-worker.v1",
            "worker": "mac-mini-browserclaw",
            "target": "E-Frontier",
            "templateId": "secretary.inbox.v1",
            "sourceRefs": [source_ref],
            "runId": f"telegram-run:{digest}",
            "idempotencyId": f"telegram-wake:{digest}",
            "deadlineMs": self._deadline_ms,
        }
        with self._lock:
            self._dispatch(request, deadline_at=time.monotonic() + self._deadline_ms / 1000)

    def _dispatch(self, request: dict[str, object], *, deadline_at: float) -> None:
        if self._session_id is None:
            self._session_id = self._initialize_session(deadline_at=deadline_at)
        try:
            result, _ = self._rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "browser_wake", "arguments": request},
                },
                session_id=self._session_id,
                deadline_at=deadline_at,
            )
        except AgentHerderWakeError:
            self._session_id = None
            raise
        if result.get("isError") is True:
            raise AgentHerderWakeError("Agent Herder rejected browser wake")
        content = result.get("content")
        if not isinstance(content, list):
            raise AgentHerderWakeError("Agent Herder returned no browser wake receipt")
        receipt: dict[str, object] | None = None
        for block in content:
            if not isinstance(block, dict) or not isinstance(block.get("text"), str):
                continue
            try:
                candidate = json.loads(block["text"])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                receipt = candidate
                break
        if receipt is None:
            raise AgentHerderWakeError("Agent Herder returned an invalid browser wake receipt")
        self._validate_record(receipt, request)
        if receipt["status"] == "failed":
            raise AgentHerderWakeError("Agent Herder browser worker failed")
        if receipt["status"] != "completed":
            raise AgentHerderWakeError("Agent Herder browser wake is not completed")

    def _initialize_session(self, *, deadline_at: float) -> str:
        _, session_id = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "universal-inbox-secretary-watch", "version": "0.1.0"},
                },
            },
            deadline_at=deadline_at,
        )
        self._post(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
            session_id=session_id,
            allow_empty=True,
            deadline_at=deadline_at,
        )
        return session_id

    def _rpc(
        self,
        message: dict[str, object],
        *,
        session_id: str | None = None,
        deadline_at: float,
    ) -> tuple[dict[str, object], str]:
        response = self._post(message, session_id=session_id, deadline_at=deadline_at)
        if response[0].get("error") is not None:
            raise AgentHerderWakeError("Agent Herder MCP request failed")
        result = response[0].get("result")
        if not isinstance(result, dict):
            raise AgentHerderWakeError("Agent Herder MCP returned no result")
        next_session = response[1] or session_id
        if not next_session:
            raise AgentHerderWakeError("Agent Herder MCP did not return a session")
        return result, next_session

    def _post(
        self,
        message: dict[str, object],
        *,
        session_id: str | None = None,
        allow_empty: bool = False,
        deadline_at: float,
    ) -> tuple[dict[str, object], str | None]:
        headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        }
        if self._token:
            headers["authorization"] = f"Bearer {self._token}"
        if session_id:
            headers["mcp-session-id"] = session_id
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise AgentHerderWakeError("Agent Herder wake deadline exceeded")
        response = self._http_post(
            Request(
                self._endpoint,
                data=json.dumps(message, separators=(",", ":")).encode("utf-8"),
                headers=headers,
                method="POST",
            ),
            remaining,
        )
        if response.status < 200 or response.status >= 300:
            raise AgentHerderWakeError(f"Agent Herder HTTP returned {response.status}")
        next_session = response.headers.get("mcp-session-id")
        if not response.body and allow_empty:
            return {}, next_session
        if not response.body:
            raise AgentHerderWakeError("Agent Herder MCP returned an empty response")
        if len(response.body) > _MAX_HTTP_RESPONSE_BYTES:
            raise AgentHerderWakeError("Agent Herder response is too large")
        return self._decode_json(response.body), next_session

    @staticmethod
    def _decode_json(body: bytes) -> dict[str, object]:
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise AgentHerderWakeError("Agent Herder MCP returned invalid JSON") from error
        if "data:" in text:
            data_lines = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
            text = next((line for line in reversed(data_lines) if line and line != "[DONE]"), "")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise AgentHerderWakeError("Agent Herder MCP returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise AgentHerderWakeError("Agent Herder MCP returned an invalid payload")
        return payload

    @staticmethod
    def _validate_source_ref(source_ref: str) -> str:
        normalized = source_ref.strip()
        if not normalized or len(normalized) > 256 or _OPAQUE_REF_RE.fullmatch(normalized) is None:
            raise ValueError("source_ref must be a bounded printable opaque ref")
        return normalized

    @classmethod
    def _validate_record(cls, record: dict[str, object], request: dict[str, object]) -> None:
        allowed = {"request", "status", "attempts", "requestedAt", "updatedAt", "receipt", "errorClass"}
        if set(record) - allowed or not {"request", "status", "attempts", "requestedAt", "updatedAt"}.issubset(record):
            raise AgentHerderWakeError("Agent Herder returned an invalid browser wake record")
        if record["request"] != request:
            raise AgentHerderWakeError("Agent Herder browser wake receipt identity mismatch")
        if type(record["attempts"]) is not int or not 1 <= record["attempts"] <= 3:
            raise AgentHerderWakeError("Agent Herder returned an invalid browser wake attempt count")
        if not isinstance(record["requestedAt"], str) or _TIMESTAMP_RE.fullmatch(record["requestedAt"]) is None:
            raise AgentHerderWakeError("Agent Herder returned an invalid browser wake timestamp")
        if not isinstance(record["updatedAt"], str) or _TIMESTAMP_RE.fullmatch(record["updatedAt"]) is None:
            raise AgentHerderWakeError("Agent Herder returned an invalid browser wake timestamp")
        status = record["status"]
        if status == "claimed":
            if "receipt" in record or "errorClass" in record:
                raise AgentHerderWakeError("Agent Herder returned an invalid claimed wake record")
            raise AgentHerderWakeError("Agent Herder browser wake is not completed")
        if status not in {"completed", "failed"}:
            raise AgentHerderWakeError("Agent Herder returned an invalid browser wake status")
        if status == "failed" and "receipt" not in record:
            if record.get("errorClass") not in _ERROR_CLASSES:
                raise AgentHerderWakeError("Agent Herder returned an invalid browser wake error class")
            return
        if status == "completed" and "errorClass" in record:
            raise AgentHerderWakeError("Agent Herder returned an invalid completed browser wake record")
        if "receipt" not in record or not isinstance(record["receipt"], dict):
            raise AgentHerderWakeError("Agent Herder returned no browser wake receipt")
        cls._validate_receipt(record["receipt"], request)
        if record["receipt"]["status"] != status:
            raise AgentHerderWakeError("Agent Herder browser wake record and receipt status mismatch")
        if status == "failed":
            error_class = record.get("errorClass")
            if error_class not in _ERROR_CLASSES:
                raise AgentHerderWakeError("Agent Herder returned an invalid browser wake error class")

    @classmethod
    def _validate_receipt(cls, receipt: dict[str, object], request: dict[str, object]) -> None:
        allowed = {"worker", "target", "templateId", "runId", "idempotencyId", "receiptRef", "status", "acceptedAt", "completedAt", "failedAt", "errorClass"}
        required = {"worker", "target", "templateId", "runId", "idempotencyId", "receiptRef", "status", "acceptedAt"}
        if set(receipt) - allowed or not required.issubset(receipt):
            raise AgentHerderWakeError("Agent Herder returned an invalid browser wake receipt")
        if (
            receipt["worker"] != "mac-mini-browserclaw"
            or receipt["target"] != "E-Frontier"
            or receipt["templateId"] != "secretary.inbox.v1"
            or receipt["runId"] != request["runId"]
            or receipt["idempotencyId"] != request["idempotencyId"]
        ):
            raise AgentHerderWakeError("Agent Herder browser wake receipt identity mismatch")
        for key in ("runId", "idempotencyId", "receiptRef"):
            if not isinstance(receipt[key], str) or len(receipt[key]) > 256 or _OPAQUE_REF_RE.fullmatch(receipt[key]) is None:
                raise AgentHerderWakeError("Agent Herder returned an invalid opaque browser wake receipt")
        for key in ("acceptedAt", "completedAt", "failedAt"):
            if key in receipt and (not isinstance(receipt[key], str) or _TIMESTAMP_RE.fullmatch(receipt[key]) is None):
                raise AgentHerderWakeError("Agent Herder returned an invalid browser wake timestamp")
        status = receipt["status"]
        if status == "completed":
            if "completedAt" not in receipt or "failedAt" in receipt or "errorClass" in receipt:
                raise AgentHerderWakeError("Agent Herder returned an invalid completed receipt")
        elif status == "failed":
            if "failedAt" not in receipt or receipt.get("errorClass") not in _ERROR_CLASSES or "completedAt" in receipt:
                raise AgentHerderWakeError("Agent Herder returned an invalid failed receipt")
        else:
            raise AgentHerderWakeError("Agent Herder returned an invalid browser wake status")
