"""Small, read-only command facade for a future MCP transport binding.

The facade deliberately contains no adapter, account, agent, or transport code.
It returns bounded factual candidates; a consumer agent is responsible for any
human-language summary or an explicitly requested action.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .adapter import AdapterManifest
from .contracts import InboxItem, ItemIdentity
from .polling import PollAttemptResult, PollFailure, PollRunResult, PollingCoordinator
from .registry import AdapterRegistry
from .store import SQLiteInboxStore


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "inputSchema": self.input_schema}


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="inbox.search",
        description="Search stored inbox previews by text.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="inbox.digest_candidates",
        description="Return the newest stored inbox previews.",
        input_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="inbox.get",
        description="Fetch one stored inbox item by source and item id.",
        input_schema={
            "type": "object",
            "properties": {"source": {"type": "string"}, "item_id": {"type": "string"}},
            "required": ["source", "item_id"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="inbox.sources.status",
        description="Return stored status for one source, if any.",
        input_schema={
            "type": "object",
            "properties": {"source": {"type": "string"}},
            "required": ["source"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="inbox.source_status",
        description="Return stored status for one source, if any.",
        input_schema={
            "type": "object",
            "properties": {"source": {"type": "string"}},
            "required": ["source"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="inbox.status",
        description="Return stored status for one source, if any.",
        input_schema={
            "type": "object",
            "properties": {"source": {"type": "string"}},
            "required": ["source"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="inbox.adapters.manifests",
        description="List registered adapter manifests.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolSpec(
        name="inbox.adapter_manifests",
        description="List registered adapter manifests.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolSpec(
        name="inbox.poll_once",
        description="Poll one registered adapter once through the coordinator.",
        input_schema={
            "type": "object",
            "properties": {
                "adapter_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "request_id": {"type": "string"},
            },
            "required": ["adapter_id"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="inbox.poll_all",
        description="Poll every registered adapter once through the coordinator.",
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "request_ids": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
            },
            "additionalProperties": False,
        },
    ),
)


class CoreMcpSurface:
    """Fail-closed core commands which a streamable MCP binding can expose."""

    def __init__(
        self,
        store: SQLiteInboxStore,
        registry: AdapterRegistry | None = None,
        coordinator: PollingCoordinator | None = None,
    ) -> None:
        self._store = store
        self._registry = registry or AdapterRegistry()
        self._coordinator = coordinator or PollingCoordinator(store, self._registry)

    def tool_manifest(self) -> dict[str, Any]:
        return {"tools": [spec.as_dict() for spec in TOOL_SPECS]}

    def tools_list(self) -> dict[str, Any]:
        return self.tool_manifest()

    def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            return {"ok": False, "error": "invalid_arguments"}
        try:
            if name == "tools/list":
                return self.tool_manifest()
            if name == "tools/call":
                return self._tools_call(arguments)
            if name == "inbox.search":
                return self._search(arguments)
            if name == "inbox.digest_candidates":
                return self._digest_candidates(arguments)
            if name == "inbox.get":
                return self._get(arguments)
            if name in {"inbox.sources.status", "inbox.source_status", "inbox.status"}:
                return self._source_status(arguments)
            if name in {"inbox.adapters.manifests", "inbox.adapter_manifests"}:
                return self._adapter_manifests()
            if name == "inbox.poll_once":
                return self._poll_once(arguments)
            if name == "inbox.poll_all":
                return self._poll_all(arguments)
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid_arguments"}
        return {"ok": False, "error": "unknown_tool"}

    def _tools_call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = arguments.get("name")
        call_arguments = arguments.get("arguments", {})
        if not isinstance(name, str) or not isinstance(call_arguments, dict):
            raise ValueError
        return self.dispatch(name, call_arguments)

    def _search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = arguments.get("query")
        limit = arguments.get("limit", 20)
        if not isinstance(query, str) or not isinstance(limit, int):
            raise ValueError
        return {"ok": True, "items": [self._preview(item) for item in self._store.search(query, limit=limit)]}

    def _digest_candidates(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = arguments.get("limit", 20)
        if not isinstance(limit, int):
            raise ValueError
        return {"ok": True, "items": [self._preview(item) for item in self._store.recent(limit=limit)]}

    def _get(self, arguments: dict[str, Any]) -> dict[str, Any]:
        source, item_id = arguments.get("source"), arguments.get("item_id")
        if not isinstance(source, str) or not isinstance(item_id, str):
            raise ValueError
        item = self._store.get(ItemIdentity(source, item_id))
        return {"ok": True, "item": self._preview(item) if item else None}

    def _source_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        source = arguments.get("source")
        if not isinstance(source, str):
            raise ValueError
        normalized_source = source.strip().lower()
        status = self._store.get_source_status(normalized_source)
        if status is None:
            adapter = self._adapter_for_source(normalized_source)
            if adapter is not None:
                status = adapter.status()
        return {"ok": True, "source": normalized_source, "status": self._serialize_source_status(status)}

    def _adapter_manifests(self) -> dict[str, Any]:
        return {"ok": True, "manifests": [self._serialize_manifest(manifest) for manifest in self._registry.manifests()]}

    def _poll_once(self, arguments: dict[str, Any]) -> dict[str, Any]:
        adapter_id = arguments.get("adapter_id")
        limit = arguments.get("limit", 100)
        request_id = arguments.get("request_id")
        if not isinstance(adapter_id, str) or not isinstance(limit, int) or limit < 1 or limit > 100:
            raise ValueError
        if request_id is not None and not isinstance(request_id, str):
            raise ValueError
        result = self._coordinator.poll_once(adapter_id, limit=limit, request_id=request_id)
        return {"ok": True, "attempt": self._serialize_poll_attempt(result)}

    def _poll_all(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = arguments.get("limit", 100)
        request_ids = arguments.get("request_ids")
        if not isinstance(limit, int) or limit < 1 or limit > 100:
            raise ValueError
        normalized_request_ids: dict[str, str] | None = None
        if request_ids is not None:
            if not isinstance(request_ids, dict):
                raise ValueError
            normalized_request_ids = {}
            for adapter_id, request_id in request_ids.items():
                if not isinstance(adapter_id, str) or not isinstance(request_id, str):
                    raise ValueError
                normalized_request_ids[adapter_id] = request_id
        result = self._coordinator.poll_all(limit=limit, request_ids=normalized_request_ids)
        return {"ok": True, "run": self._serialize_poll_run(result)}

    @staticmethod
    def _preview(item: InboxItem) -> dict[str, str | None]:
        return {
            "ref": f"inbox://{item.identity.source}/{item.identity.item_id}",
            "source": item.identity.source,
            "item_id": item.identity.item_id,
            "title": item.title,
            "body": item.body,
            "content_state": item.content_state.value,
        }

    @staticmethod
    def _serialize_manifest(manifest: AdapterManifest) -> dict[str, Any]:
        return {
            "adapter_id": manifest.adapter_id,
            "source": manifest.source,
            "capabilities": [capability.value for capability in sorted(manifest.capabilities, key=lambda capability: capability.value)],
        }

    def _adapter_for_source(self, source: str):
        for adapter in self._registry.adapters():
            if adapter.manifest.source == source:
                return adapter
        return None

    def _serialize_source_status(self, status: object) -> dict[str, Any] | None:
        if status is None:
            return None
        if not isinstance(status, object):
            return None
        source_status = status
        accepted_item_ids = getattr(source_status, "accepted_item_ids", ())
        return {
            "source": getattr(source_status, "source", None),
            "adapter_id": getattr(source_status, "adapter_id", None),
            "cursor": self._serialize_cursor(getattr(source_status, "cursor", None)),
            "item_count": getattr(source_status, "item_count", None),
            "last_attempted_at": getattr(source_status, "last_attempted_at", None),
            "last_success_at": getattr(source_status, "last_success_at", None),
            "last_request_id": getattr(source_status, "last_request_id", None),
            "last_receipt_request_id": getattr(source_status, "last_receipt_request_id", None),
            "error_class": getattr(source_status, "error_class", None),
            "retry_after_seconds": getattr(source_status, "retry_after_seconds", None),
            "accepted_item_ids": [self._serialize_identity(identity) for identity in accepted_item_ids],
        }

    @staticmethod
    def _serialize_cursor(cursor: object) -> dict[str, Any] | None:
        if cursor is None:
            return None
        return {
            "value": getattr(cursor, "value", None),
            "source": getattr(cursor, "source", None),
        }

    @staticmethod
    def _serialize_identity(identity: ItemIdentity) -> dict[str, str]:
        return {"source": identity.source, "item_id": identity.item_id}

    def _serialize_poll_attempt(self, attempt: PollAttemptResult) -> dict[str, Any]:
        return {
            "adapter_id": attempt.adapter_id,
            "source": attempt.source,
            "request_id": attempt.request_id,
            "accepted_item_ids": [self._serialize_identity(identity) for identity in attempt.accepted_item_ids],
            "inserted_item_count": attempt.inserted_item_count,
            "item_count": attempt.item_count,
            "accepted_cursor": self._serialize_cursor(attempt.accepted_cursor),
            "receipt_recorded": attempt.receipt_recorded,
            "failure": self._serialize_poll_failure(attempt.failure),
        }

    @staticmethod
    def _serialize_poll_failure(failure: PollFailure | None) -> dict[str, Any] | None:
        if failure is None:
            return None
        return {
            "adapter_id": failure.adapter_id,
            "source": failure.source,
            "request_id": failure.request_id,
            "error_class": failure.error_class,
            "retry_after_seconds": failure.retry_after_seconds,
            "message": failure.message,
        }

    def _serialize_poll_run(self, run: PollRunResult) -> dict[str, Any]:
        return {
            "attempts": [self._serialize_poll_attempt(attempt) for attempt in run.attempts],
            "partial_failures": [self._serialize_poll_failure(failure) for failure in run.partial_failures],
        }
