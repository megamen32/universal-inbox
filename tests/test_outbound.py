from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone

import pytest

from universal_inbox.contracts import ExplicitAction, ItemIdentity
from universal_inbox.outbound import (
    HumanConfirmationGate,
    JsonlEvidenceLedger,
    OutboundConfirmationError,
    OutboundDraft,
    OutboundAuthorization,
)


def _draft(*, text: str = "Отправить это") -> OutboundDraft:
    created = datetime.now(timezone.utc)
    return OutboundDraft(
        source="telegram",
        item_id="-1001",
        kind="send",
        note=text,
        created_at=created,
        expires_at=created + timedelta(minutes=10),
    )


def test_send_action_requires_evidence_and_exact_payload(tmp_path) -> None:
    draft = _draft()
    with pytest.raises(ValueError):
        ExplicitAction(identity=ItemIdentity("telegram", "-1001"), kind="send", note=draft.note)

    gate = HumanConfirmationGate(JsonlEvidenceLedger(tmp_path / "evidence.jsonl"))
    gate.prepare(draft)
    authorization = gate.confirm(
        draft,
        human_response=" да ",
        harness="pytest",
        session_id="session-1",
        now=draft.created_at + timedelta(minutes=1),
    )
    action = ExplicitAction(
        identity=ItemIdentity("telegram", "-1001"),
        kind="send",
        note=draft.note,
        authorization=authorization,
    )
    assert action.action_hash == draft.action_hash
    accepted = gate._ledger.find(request_id=draft.request_id, event_type="confirmation_accepted")
    assert accepted["raw_response"] == " да "


def test_rejection_is_recorded_and_never_authorizes(tmp_path) -> None:
    ledger = JsonlEvidenceLedger(tmp_path / "evidence.jsonl")
    gate = HumanConfirmationGate(ledger)
    draft = _draft()
    gate.prepare(draft)

    with pytest.raises(OutboundConfirmationError):
        gate.confirm(draft, human_response="да, наверное", harness="pytest", session_id="s1")

    assert ledger.find(request_id=draft.request_id, event_type="confirmation_rejected") is not None
    assert ledger.find(request_id=draft.request_id, event_type="confirmation_accepted") is None


def test_payload_change_and_replay_fail_closed(tmp_path) -> None:
    ledger = JsonlEvidenceLedger(tmp_path / "evidence.jsonl")
    gate = HumanConfirmationGate(ledger)
    draft = gate.prepare(_draft())
    changed = OutboundDraft(
        source=draft.source,
        item_id=draft.item_id,
        kind=draft.kind,
        note="другой текст",
        request_id=draft.request_id,
        created_at=draft.created_at,
        expires_at=draft.expires_at,
    )
    with pytest.raises(OutboundConfirmationError):
        gate.confirm(changed, human_response="да", harness="pytest", session_id="s1")

    authorization = gate.confirm(
        draft,
        human_response="да",
        harness="pytest",
        session_id="s1",
        now=draft.created_at + timedelta(minutes=1),
    )
    assert ledger.find(request_id=draft.request_id, event_type="confirmation_accepted")["evidence_id"] == authorization.evidence.evidence_id
    with pytest.raises(OutboundConfirmationError):
        gate.confirm(draft, human_response="да", harness="pytest", session_id="s1")


def test_authorization_is_gate_issued_and_expiry_is_checked_again(tmp_path) -> None:
    ledger = JsonlEvidenceLedger(tmp_path / "evidence.jsonl")
    gate = HumanConfirmationGate(ledger)
    draft = gate.prepare(_draft())
    authorization = gate.confirm(
        draft,
        human_response="да",
        harness="pytest",
        session_id="s1",
        now=draft.created_at + timedelta(minutes=1),
    )

    gate.verify(authorization, now=draft.created_at + timedelta(minutes=2))
    with pytest.raises(OutboundConfirmationError):
        gate.verify(authorization, now=draft.created_at + timedelta(minutes=11))
    with pytest.raises(ValueError):
        OutboundAuthorization(draft, authorization.evidence, object(), lambda **_: None)


def test_ledger_is_private_and_durable(tmp_path) -> None:
    path = tmp_path / "evidence.jsonl"
    ledger = JsonlEvidenceLedger(path)
    ledger.append({"event_type": "prepared", "request_id": "r1"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["request_id"] == "r1"
