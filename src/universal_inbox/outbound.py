"""Durable human evidence required before provider outbound actions."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable


class OutboundConfirmationError(RuntimeError):
    """A write was refused because its human evidence was insufficient."""


@dataclass(frozen=True, slots=True)
class OutboundDraft:
    source: str
    item_id: str
    kind: str
    note: str
    payload: tuple[tuple[str, str], ...] = ()
    request_id: str = ""
    created_at: datetime | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.source or not self.item_id or not self.kind or not self.note:
            raise ValueError("outbound draft identity, kind, and note are required")
        created_at = self.created_at or datetime.now(timezone.utc)
        expires_at = self.expires_at or created_at + timedelta(minutes=10)
        if created_at.tzinfo is None or expires_at.tzinfo is None or expires_at <= created_at:
            raise ValueError("outbound draft timestamps must be timezone-aware and ordered")
        object.__setattr__(self, "request_id", self.request_id or str(uuid.uuid4()))
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "expires_at", expires_at)

    @property
    def canonical_payload(self) -> dict[str, object]:
        return {
            "source": self.source,
            "item_id": self.item_id,
            "kind": self.kind,
            "note": self.note,
            "payload": [[key, value] for key, value in self.payload],
        }

    @property
    def action_hash(self) -> str:
        encoded = json.dumps(self.canonical_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class OutboundEvidence:
    request_id: str
    action_hash: str
    raw_response: str
    normalized_response: str
    harness: str
    session_id: str
    actor: str
    confirmed_at: datetime
    evidence_id: str = ""

    def __post_init__(self) -> None:
        if self.normalized_response != "да":
            raise ValueError("outbound evidence requires exact 'да'")
        if not self.action_hash or not self.harness or not self.session_id or not self.actor:
            raise ValueError("outbound evidence context is incomplete")
        if self.confirmed_at.tzinfo is None:
            raise ValueError("outbound evidence must have a timezone-aware timestamp")
        object.__setattr__(self, "evidence_id", self.evidence_id or secrets.token_hex(16))


_ISSUE_TOKEN = object()


@dataclass(frozen=True, slots=True)
class OutboundAuthorization:
    """Opaque gate-issued capability accepted by an outbound adapter."""

    draft: OutboundDraft
    evidence: OutboundEvidence
    _issue_token: object = field(repr=False, compare=False)
    _verify: Callable[..., None] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issue_token is not _ISSUE_TOKEN:
            raise ValueError("authorization must be issued by HumanConfirmationGate")

    def verify(self, *, now: datetime | None = None) -> None:
        self._verify(self.draft, self.evidence, now=now)


class JsonlEvidenceLedger:
    """Private append-only ledger; writes are fsync'd before release."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def append(self, event: dict[str, object]) -> None:
        payload = (json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self._path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "ab", closefd=False) as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def find(self, *, request_id: str, event_type: str) -> dict[str, object] | None:
        try:
            with self._path.open("rb") as stream:
                for line in stream:
                    event = json.loads(line)
                    if event.get("request_id") == request_id and event.get("event_type") == event_type:
                        return event
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, TypeError) as error:
            raise OutboundConfirmationError("evidence ledger is unavailable or corrupt") from error
        return None

    def append_once(self, event: dict[str, object], *, unique_event_type: str) -> bool:
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self._path, flags, 0o600)
        payload = (json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "a+b", closefd=False) as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                stream.seek(0)
                for line in stream:
                    existing = json.loads(line)
                    if existing.get("request_id") == event.get("request_id") and existing.get("event_type") == unique_event_type:
                        return False
                stream.seek(0, os.SEEK_END)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                return True
        except (OSError, json.JSONDecodeError, TypeError) as error:
            raise OutboundConfirmationError("evidence ledger is unavailable or corrupt") from error
        finally:
            os.close(descriptor)


class HumanConfirmationGate:
    """Create durable evidence; adapters must verify it before a write."""

    def __init__(self, ledger: JsonlEvidenceLedger) -> None:
        self._ledger = ledger

    def prepare(self, draft: OutboundDraft) -> OutboundDraft:
        self._ledger.append(
            {
                "event_type": "prepared",
                "request_id": draft.request_id,
                "payload": draft.canonical_payload,
                "action_hash": draft.action_hash,
                "created_at": draft.created_at.isoformat(),
                "expires_at": draft.expires_at.isoformat(),
            }
        )
        return draft

    def confirm(
        self,
        draft: OutboundDraft,
        *,
        human_response: str,
        harness: str,
        session_id: str,
        actor: str = "human",
        now: datetime | None = None,
    ) -> OutboundAuthorization:
        prepared = self._ledger.find(request_id=draft.request_id, event_type="prepared")
        if prepared is None:
            raise OutboundConfirmationError("outbound draft has no durable prepare evidence")
        if prepared.get("action_hash") != draft.action_hash or prepared.get("payload") != draft.canonical_payload:
            raise OutboundConfirmationError("outbound draft differs from prepared evidence")
        if self._ledger.find(request_id=draft.request_id, event_type="confirmation_accepted") is not None:
            raise OutboundConfirmationError("outbound draft was already confirmed")
        confirmed_at = now or datetime.now(timezone.utc)
        if len(human_response.encode("utf-8")) > 4096:
            raise OutboundConfirmationError("human confirmation response is too large")
        normalized = human_response.strip().casefold()
        if normalized != "да":
            self._ledger.append(
                {
                    "event_type": "confirmation_rejected",
                    "request_id": draft.request_id,
                    "action_hash": draft.action_hash,
                    "raw_response": human_response,
                    "normalized_response": normalized,
                    "harness": harness,
                    "session_id": session_id,
                    "actor": actor,
                    "confirmed_at": confirmed_at.isoformat(),
                }
            )
            raise OutboundConfirmationError("human confirmation must be exactly 'да'")
        if confirmed_at > draft.expires_at:
            raise OutboundConfirmationError("outbound draft confirmation expired")
        evidence = OutboundEvidence(
            request_id=draft.request_id,
            action_hash=draft.action_hash,
            raw_response=human_response,
            normalized_response=normalized,
            harness=harness,
            session_id=session_id,
            actor=actor,
            confirmed_at=confirmed_at,
        )
        accepted = self._ledger.append_once(
            {
                "event_type": "confirmation_accepted",
                "request_id": draft.request_id,
                "evidence_id": evidence.evidence_id,
                "payload": draft.canonical_payload,
                "action_hash": draft.action_hash,
                "raw_response": evidence.raw_response,
                "normalized_response": evidence.normalized_response,
                "harness": evidence.harness,
                "session_id": evidence.session_id,
                "actor": evidence.actor,
                "confirmed_at": evidence.confirmed_at.isoformat(),
            },
            unique_event_type="confirmation_accepted",
        )
        if not accepted:
            raise OutboundConfirmationError("outbound draft was already confirmed")
        return self.authorize(draft, evidence, now=confirmed_at)

    def authorize(
        self,
        draft: OutboundDraft,
        evidence: OutboundEvidence,
        *,
        now: datetime | None = None,
    ) -> OutboundAuthorization:
        accepted = self._ledger.find(request_id=draft.request_id, event_type="confirmation_accepted")
        checked_at = now or datetime.now(timezone.utc)
        if (
            accepted is None
            or checked_at > draft.expires_at
            or accepted.get("evidence_id") != evidence.evidence_id
            or accepted.get("action_hash") != draft.action_hash
            or evidence.request_id != draft.request_id
            or evidence.action_hash != draft.action_hash
        ):
            raise OutboundConfirmationError("outbound evidence is not durable for this exact draft")
        try:
            confirmed_at = datetime.fromisoformat(str(accepted["confirmed_at"]))
        except (KeyError, TypeError, ValueError) as error:
            raise OutboundConfirmationError("outbound evidence timestamp is invalid") from error
        if confirmed_at > draft.expires_at or evidence.confirmed_at != confirmed_at:
            raise OutboundConfirmationError("outbound evidence is expired or altered")
        return OutboundAuthorization(
            draft=draft,
            evidence=evidence,
            _issue_token=_ISSUE_TOKEN,
            _verify=self._verify_durable,
        )

    def verify(self, authorization: OutboundAuthorization, *, now: datetime | None = None) -> None:
        authorization.verify(now=now)

    def _verify_durable(
        self,
        draft: OutboundDraft,
        evidence: OutboundEvidence,
        *,
        now: datetime | None = None,
    ) -> None:
        self.authorize(draft, evidence, now=now)
