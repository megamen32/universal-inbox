"""Read-only Gmail adapters, including the local Himalaya command seam."""

from __future__ import annotations

from dataclasses import dataclass
import json
import subprocess
from typing import Protocol

from ..adapter import TransientAdapterError
from ..contracts import InboxCursor, InboxItem, ItemIdentity
from ._read_only import ReadOnlyInboxAdapter, ReadOnlyPage


@dataclass(frozen=True, slots=True)
class GmailPreview:
    message_id: str
    subject: str | None = None
    snippet: str | None = None
    cursor: str | None = None
    sender: str | None = None
    received_at: str | None = None


def _gmail_item_mapper(record: GmailPreview, source: str) -> InboxItem:
    identity = ItemIdentity(source=source, item_id=record.message_id)
    return InboxItem(
        identity=identity,
        title=record.subject,
        body=record.snippet or _preview_body(record),
        cursor=InboxCursor(record.cursor, source=source) if record.cursor is not None else None,
    )


def _preview_body(record: GmailPreview) -> str | None:
    parts = [value for value in (record.sender, record.received_at) if value]
    return " · ".join(parts) or None


class GmailCommandRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        max_stdout_bytes: int,
    ) -> str: ...


class SubprocessGmailCommandRunner:
    """Bounded argv-only runner for the user's already-configured Himalaya."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        max_stdout_bytes: int,
    ) -> str:
        try:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                shell=False,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TransientAdapterError("Himalaya command did not complete") from exc
        if completed.returncode != 0:
            raise TransientAdapterError("Himalaya command failed")
        if len(completed.stdout) > max_stdout_bytes:
            raise TransientAdapterError("Himalaya response exceeded the configured limit")
        try:
            return completed.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TransientAdapterError("Himalaya response was not UTF-8") from exc


class GmailHimalayaReader:
    """Read a bounded newest-first Himalaya envelope snapshot without writes."""

    def __init__(
        self,
        runner: GmailCommandRunner | None = None,
        *,
        binary: str = "himalaya",
        account: str = "gmail",
        mailbox: str = "Inbox",
        snapshot_size: int = 100,
        timeout_seconds: float = 20.0,
        max_stdout_bytes: int = 2_000_000,
    ) -> None:
        if not binary.strip() or account not in {"gmail", "careviolan"} or mailbox not in {"Inbox", "[Gmail]/Спам"}:
            raise ValueError("Gmail command, account, and mailbox must be allowlisted")
        if snapshot_size < 1 or timeout_seconds <= 0 or max_stdout_bytes < 1:
            raise ValueError("Gmail reader bounds must be positive")
        self._runner = runner or SubprocessGmailCommandRunner()
        self._binary = binary
        self._account = account.strip()
        self._mailbox = mailbox
        self._snapshot_size = snapshot_size
        self._timeout_seconds = timeout_seconds
        self._max_stdout_bytes = max_stdout_bytes

    def __call__(self, cursor: InboxCursor | None, limit: int) -> ReadOnlyPage[GmailPreview]:
        if limit < 1:
            raise ValueError("limit must be positive")
        if cursor is not None and cursor.source not in {None, "gmail"}:
            raise ValueError("Gmail cursor source mismatch")
        output = self._runner.run(
            (
                self._binary,
                "-a",
                self._account,
                "--json",
                "envelope",
                "list",
                "-m",
                self._mailbox,
                "-p",
                "1",
                "-s",
                str(max(self._snapshot_size, limit)),
            ),
            timeout_seconds=self._timeout_seconds,
            max_stdout_bytes=self._max_stdout_bytes,
        )
        try:
            envelopes = json.loads(output)["envelopes"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise TransientAdapterError("Himalaya returned invalid envelope JSON") from exc
        if not isinstance(envelopes, list) or not all(isinstance(envelope, dict) for envelope in envelopes):
            raise TransientAdapterError("Himalaya envelopes must be objects")

        newest_first: list[GmailPreview] = []
        seen: set[str] = set()
        found_cursor = cursor is None
        for envelope in envelopes:
            preview = self._preview(envelope)
            if preview.message_id in seen:
                raise TransientAdapterError("Himalaya returned duplicate Gmail identities")
            seen.add(preview.message_id)
            if cursor is not None and preview.message_id == cursor.value:
                found_cursor = True
                break
            newest_first.append(preview)
        if not found_cursor:
            raise TransientAdapterError("Gmail cursor is outside the bounded envelope snapshot")

        chronological = list(reversed(newest_first))
        selected = chronological[:limit]
        next_cursor = selected[-1].message_id if selected else (cursor.value if cursor else None)
        return ReadOnlyPage(items=tuple(selected), next_cursor=next_cursor)

    @staticmethod
    def _preview(envelope: dict[str, object]) -> GmailPreview:
        message_id = envelope.get("message-id") or envelope.get("id")
        subject = envelope.get("subject")
        date = envelope.get("date")
        senders = envelope.get("from") or []
        if not isinstance(message_id, str) or not message_id.strip():
            raise TransientAdapterError("Gmail envelope has no stable identity")
        if subject is not None and not isinstance(subject, str):
            raise TransientAdapterError("Gmail subject is malformed")
        if date is not None and not isinstance(date, str):
            raise TransientAdapterError("Gmail date is malformed")
        sender = None
        if isinstance(senders, list) and senders:
            first = senders[0]
            if isinstance(first, dict):
                name, email = first.get("name"), first.get("email")
                if isinstance(email, str) and email:
                    sender = f"{name} <{email}>" if isinstance(name, str) and name else email
        return GmailPreview(
            message_id=message_id.strip(),
            subject=subject.strip() if isinstance(subject, str) and subject.strip() else None,
            cursor=message_id.strip(),
            sender=sender,
            received_at=date,
        )


class GmailReadAdapter(ReadOnlyInboxAdapter[GmailPreview]):
    def __init__(
        self,
        *,
        adapter_id: str,
        reader,
        source: str = "gmail",
        capabilities=(),
    ) -> None:
        super().__init__(
            adapter_id=adapter_id,
            source=source,
            reader=reader,
            item_mapper=_gmail_item_mapper,
            capabilities=capabilities,
        )
