from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from typing import Iterable

from .outbound import OutboundAuthorization


def _normalize_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


class ContentState(Enum):
    PRESENT = "present"
    TOMBSTONED = "tombstoned"


class Capability(Enum):
    POLL = "poll"
    SEARCH = "search"
    EXPLICIT_ACTION = "explicit_action"


@dataclass(frozen=True, slots=True)
class ItemIdentity:
    source: str
    item_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _normalize_text(self.source, "source").lower())
        object.__setattr__(self, "item_id", _normalize_text(self.item_id, "item_id"))


@dataclass(frozen=True, slots=True)
class InboxCursor:
    value: str
    source: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _normalize_text(self.value, "value"))
        if self.source is not None:
            object.__setattr__(self, "source", _normalize_text(self.source, "source").lower())


@dataclass(frozen=True, slots=True)
class ItemRef:
    identity: ItemIdentity
    label: str | None = None

    def __post_init__(self) -> None:
        if self.label is not None:
            object.__setattr__(self, "label", _normalize_text(self.label, "label"))


@dataclass(frozen=True, slots=True)
class InboxItem:
    identity: ItemIdentity
    refs: tuple[ItemRef, ...] = ()
    content_state: ContentState = ContentState.PRESENT
    title: str | None = None
    body: str | None = None
    sender: str | None = None
    cursor: InboxCursor | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "refs", tuple(self.refs))
        if self.title is not None:
            object.__setattr__(self, "title", _normalize_text(self.title, "title"))
        if self.body is not None:
            object.__setattr__(self, "body", self.body.strip())
        if self.sender is not None:
            object.__setattr__(self, "sender", _normalize_text(self.sender, "sender"))
        if self.cursor is not None and not isinstance(self.cursor, InboxCursor):
            raise TypeError("cursor must be an InboxCursor or None")
        for ref in self.refs:
            if ref.identity != self.identity:
                raise ValueError("all refs must match the item identity")

    @property
    def is_tombstoned(self) -> bool:
        return self.content_state is ContentState.TOMBSTONED


@dataclass(frozen=True, slots=True)
class PollBatch:
    items: tuple[InboxItem, ...] = ()
    next_cursor: InboxCursor | None = None
    capabilities: frozenset[Capability] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        if self.next_cursor is not None and not isinstance(self.next_cursor, InboxCursor):
            raise TypeError("next_cursor must be an InboxCursor or None")
        for item in self.items:
            if not isinstance(item, InboxItem):
                raise TypeError("items must contain InboxItem instances")


@dataclass(frozen=True, slots=True)
class ExplicitAction:
    identity: ItemIdentity
    kind: str
    note: str | None = None
    payload: tuple[tuple[str, str], ...] = ()
    authorization: OutboundAuthorization | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _normalize_text(self.kind, "kind").lower())
        if self.note is not None:
            object.__setattr__(self, "note", self.note.strip())
        normalized_payload = tuple((str(key), str(value)) for key, value in self.payload)
        object.__setattr__(self, "payload", normalized_payload)
        if self.kind in {"send", "reply", "comment", "publish"}:
            if not self.note or self.authorization is None:
                raise ValueError("outbound actions require exact human confirmation evidence")
            self.authorization.verify()
            if self.authorization.draft.action_hash != self.action_hash:
                raise ValueError("confirmation evidence does not match the exact action payload")

    def verify_for_execution(self) -> None:
        """Recheck durable evidence immediately before a provider write."""

        if self.kind in {"send", "reply", "comment", "publish"}:
            if self.authorization is None:
                raise ValueError("outbound action has no authorization")
            self.authorization.verify()

    @property
    def canonical_payload(self) -> dict[str, object]:
        return {
            "source": self.identity.source,
            "item_id": self.identity.item_id,
            "kind": self.kind,
            "note": self.note or "",
            "payload": [[key, value] for key, value in self.payload],
        }

    @property
    def action_hash(self) -> str:
        encoded = json.dumps(self.canonical_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    action: ExplicitAction
    accepted: bool
    receipt_id: str | None = None
    item_identity: ItemIdentity | None = None
    outcome: str | None = None

    def __post_init__(self) -> None:
        if self.item_identity is None:
            object.__setattr__(self, "item_identity", self.action.identity)
        elif self.item_identity != self.action.identity:
            raise ValueError("item_identity must match the action identity")
        if self.receipt_id is not None:
            object.__setattr__(self, "receipt_id", _normalize_text(self.receipt_id, "receipt_id"))
        if self.accepted and self.receipt_id is None:
            raise ValueError("accepted receipts must include a receipt_id")
        if self.outcome is not None:
            object.__setattr__(self, "outcome", self.outcome.strip())


@dataclass(frozen=True, slots=True)
class SourceStatus:
    source: str
    adapter_id: str
    cursor: InboxCursor | None = None
    item_count: int = 0
    last_attempted_at: str | None = None
    last_success_at: str | None = None
    last_request_id: str | None = None
    last_receipt_request_id: str | None = None
    error_class: str | None = None
    retry_after_seconds: int | None = None
    accepted_item_ids: tuple[ItemIdentity, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _normalize_text(self.source, "source").lower())
        object.__setattr__(self, "adapter_id", _normalize_text(self.adapter_id, "adapter_id"))
        object.__setattr__(self, "accepted_item_ids", tuple(self.accepted_item_ids))
        if self.cursor is not None and not isinstance(self.cursor, InboxCursor):
            raise TypeError("cursor must be an InboxCursor or None")
        if self.item_count < 0:
            raise ValueError("item_count must be non-negative")
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must be non-negative")
        for identity in self.accepted_item_ids:
            if identity.source != self.source:
                raise ValueError("accepted_item_ids must belong to the source")


@dataclass(frozen=True, slots=True)
class PollReceipt:
    source: str
    adapter_id: str
    request_id: str
    accepted_item_ids: tuple[ItemIdentity, ...] = ()
    accepted_cursor: InboxCursor | None = None
    item_count: int = 0
    error_class: str | None = None
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _normalize_text(self.source, "source").lower())
        object.__setattr__(self, "adapter_id", _normalize_text(self.adapter_id, "adapter_id"))
        object.__setattr__(self, "request_id", _normalize_text(self.request_id, "request_id"))
        object.__setattr__(self, "accepted_item_ids", tuple(self.accepted_item_ids))
        if self.accepted_cursor is not None and not isinstance(self.accepted_cursor, InboxCursor):
            raise TypeError("accepted_cursor must be an InboxCursor or None")
        if self.item_count < 0:
            raise ValueError("item_count must be non-negative")
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must be non-negative")
        for identity in self.accepted_item_ids:
            if identity.source != self.source:
                raise ValueError("accepted_item_ids must belong to the source")


def build_poll_batch(
    items: Iterable[InboxItem],
    next_cursor: InboxCursor | None = None,
    capabilities: Iterable[Capability] = (),
) -> PollBatch:
    return PollBatch(items=tuple(items), next_cursor=next_cursor, capabilities=frozenset(capabilities))
