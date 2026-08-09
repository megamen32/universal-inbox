"""Adapter protocol and manifest for Universal Inbox polling sources."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .contracts import ActionReceipt, Capability, ExplicitAction, InboxCursor, InboxItem, ItemIdentity, PollBatch, SourceStatus


def _normalize_adapter_id(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("adapter_id must not be empty")
    return normalized


@dataclass(frozen=True, slots=True)
class AdapterManifest:
    adapter_id: str
    source: str
    capabilities: frozenset[Capability] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "adapter_id", _normalize_adapter_id(self.adapter_id))
        object.__setattr__(self, "source", self.source.strip().lower())
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        if not self.source:
            raise ValueError("source must not be empty")


class AdapterError(Exception):
    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class TransientAdapterError(AdapterError):
    pass


class PermanentAdapterError(AdapterError):
    pass


class MalformedAdapterResponse(AdapterError):
    pass


@runtime_checkable
class InboxAdapter(Protocol):
    manifest: AdapterManifest

    def status(self) -> SourceStatus: ...

    def poll(self, cursor: InboxCursor | None, *, limit: int) -> PollBatch: ...

    def get(self, identity: ItemIdentity) -> InboxItem | None: ...

    # Outbound adapters must call action.verify_for_execution() before writing.
    def execute(self, action: ExplicitAction) -> ActionReceipt: ...
