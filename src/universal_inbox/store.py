"""Durable local storage for the canonical Universal Inbox contracts."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .contracts import (
    ActionReceipt,
    ContentState,
    InboxCursor,
    InboxItem,
    ItemIdentity,
    ItemRef,
    PollReceipt,
    SourceStatus,
)


class ItemConflictError(ValueError):
    """A source reused an identity with incompatible immutable payload."""


class SQLiteInboxStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS items (
                    source TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    title TEXT,
                    body TEXT,
                    content_state TEXT NOT NULL,
                    cursor_value TEXT,
                    refs_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (source, item_id)
                );
                CREATE TABLE IF NOT EXISTS source_cursors (
                    source TEXT PRIMARY KEY,
                    cursor_value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS action_receipts (
                    source TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    action_kind TEXT NOT NULL,
                    receipt_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (source, item_id, action_kind, receipt_id)
                );
                CREATE TABLE IF NOT EXISTS source_statuses (
                    source TEXT PRIMARY KEY,
                    adapter_id TEXT NOT NULL,
                    cursor_value TEXT,
                    item_count INTEGER NOT NULL,
                    last_attempted_at TEXT,
                    last_success_at TEXT,
                    last_request_id TEXT,
                    last_receipt_request_id TEXT,
                    error_class TEXT,
                    retry_after_seconds INTEGER,
                    accepted_item_ids_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS poll_receipts (
                    source TEXT NOT NULL,
                    adapter_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    accepted_cursor_value TEXT,
                    accepted_item_ids_json TEXT NOT NULL,
                    item_count INTEGER NOT NULL,
                    error_class TEXT,
                    retry_after_seconds INTEGER,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (source, adapter_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS wake_outbox (
                    source TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    ref TEXT NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    claimed_by TEXT,
                    claimed_until REAL,
                    claim_epoch INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (source, item_id)
                );
                """
            )
            columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(wake_outbox)")}
            if "claimed_by" not in columns:
                self._connection.execute("ALTER TABLE wake_outbox ADD COLUMN claimed_by TEXT")
            if "claimed_until" not in columns:
                self._connection.execute("ALTER TABLE wake_outbox ADD COLUMN claimed_until REAL")
            if "claim_epoch" not in columns:
                self._connection.execute("ALTER TABLE wake_outbox ADD COLUMN claim_epoch INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SQLiteInboxStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def counts(self) -> dict[str, int]:
        return {
            "items": self._count("items"),
            "receipts": self._count("action_receipts"),
            "sources": self._count("source_cursors"),
        }

    def get_source_status(self, source: str) -> SourceStatus | None:
        row = self._connection.execute(
            "SELECT * FROM source_statuses WHERE source = ?", (source.strip().lower(),)
        ).fetchone()
        return None if row is None else self._row_to_source_status(row)

    def list_poll_receipts(self, source: str | None = None) -> list[PollReceipt]:
        if source is None:
            rows = self._connection.execute("SELECT * FROM poll_receipts ORDER BY rowid").fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM poll_receipts WHERE source = ? ORDER BY rowid",
                (source.strip().lower(),),
            ).fetchall()
        return [self._row_to_poll_receipt(row) for row in rows]

    def get_source_cursor(self, source: str) -> InboxCursor | None:
        row = self._connection.execute(
            "SELECT cursor_value FROM source_cursors WHERE source = ?", (source.strip().lower(),)
        ).fetchone()
        return None if row is None else InboxCursor(row["cursor_value"], source=source)

    def ingest(self, item: InboxItem, *, advance_cursor: bool = True) -> bool:
        payload = self._item_payload(item)
        encoded = self._encode(payload)
        source, item_id = item.identity.source, item.identity.item_id
        with self._connection:
            row = self._connection.execute(
                "SELECT payload_json FROM items WHERE source = ? AND item_id = ?", (source, item_id)
            ).fetchone()
            if row is not None:
                if row["payload_json"] != encoded:
                    raise ItemConflictError(f"conflicting item for {source}/{item_id}")
                if advance_cursor and item.cursor is not None:
                    self._connection.execute(
                        "UPDATE items SET cursor_value = ? WHERE source = ? AND item_id = ?",
                        (item.cursor.value, source, item_id),
                    )
                if advance_cursor:
                    self._upsert_cursor(item.cursor)
                return False
            self._connection.execute(
                """
                INSERT INTO items(source, item_id, title, body, content_state, cursor_value, refs_json, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    item_id,
                    item.title,
                    item.body,
                    item.content_state.value,
                    item.cursor.value if item.cursor else None,
                    self._encode(payload["refs"]),
                    encoded,
                ),
            )
            if advance_cursor:
                self._upsert_cursor(item.cursor)
            return True

    def claim_wake(
        self,
        identity: ItemIdentity,
        ref: str,
        owner: str,
        *,
        lease_seconds: float = 120.0,
    ) -> tuple[str, bool, bool, int]:
        """Persist and atomically claim one wake for one dispatcher owner.

        Returns ``(ref, delivered, claimed)``. A false ``claimed`` means
        another live dispatcher owns the lease and the source cursor must not
        advance past this item.
        """
        if not ref.strip():
            raise ValueError("wake ref must not be empty")
        if not owner.strip() or lease_seconds <= 0:
            raise ValueError("wake owner and lease must be valid")
        now = time.time()
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO wake_outbox(source, item_id, ref) VALUES (?, ?, ?)",
                (identity.source, identity.item_id, ref),
            )
            row = self._connection.execute(
                "SELECT ref, delivered, claimed_by, claimed_until, claim_epoch FROM wake_outbox WHERE source=? AND item_id=?",
                (identity.source, identity.item_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("wake outbox row was not persisted")
            if row["delivered"]:
                return str(row["ref"]), True, False, int(row["claim_epoch"])
            current_owner = row["claimed_by"]
            claimed_until = row["claimed_until"]
            if current_owner and current_owner != owner and claimed_until and float(claimed_until) > now:
                return str(row["ref"]), False, False, int(row["claim_epoch"])
            claim_epoch = int(row["claim_epoch"]) + 1
            self._connection.execute(
                "UPDATE wake_outbox SET claimed_by=?, claimed_until=?, claim_epoch=? WHERE source=? AND item_id=? AND delivered=0",
                (owner, now + lease_seconds, claim_epoch, identity.source, identity.item_id),
            )
        return str(row["ref"]), False, True, claim_epoch

    def mark_wake_delivered(self, identity: ItemIdentity, owner: str, claim_epoch: int) -> bool:
        with self._connection:
            result = self._connection.execute(
                "UPDATE wake_outbox SET delivered=1, claimed_by=NULL, claimed_until=NULL WHERE source=? AND item_id=? AND claimed_by=? AND claim_epoch=? AND delivered=0",
                (identity.source, identity.item_id, owner, claim_epoch),
            )
        return result.rowcount == 1

    def release_wake_claim(self, identity: ItemIdentity, owner: str, claim_epoch: int) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE wake_outbox SET claimed_by=NULL, claimed_until=NULL WHERE source=? AND item_id=? AND claimed_by=? AND claim_epoch=? AND delivered=0",
                (identity.source, identity.item_id, owner, claim_epoch),
            )

    def advance_source_cursor(self, source: str, cursor: InboxCursor | None) -> None:
        if cursor is None:
            return
        normalized_source = source.strip().lower()
        if cursor.source is not None and cursor.source != normalized_source:
            raise ValueError("cursor source must match the provided source")
        persisted = cursor if cursor.source == normalized_source else InboxCursor(cursor.value, source=normalized_source)
        with self._connection:
            self._upsert_cursor(persisted)

    def record_source_status(self, status: SourceStatus) -> bool:
        payload = self._encode(self._source_status_payload(status))
        values = (
            status.source,
            status.adapter_id,
            status.cursor.value if status.cursor else None,
            status.item_count,
            status.last_attempted_at,
            status.last_success_at,
            status.last_request_id,
            status.last_receipt_request_id,
            status.error_class,
            status.retry_after_seconds,
            self._encode([self._identity_payload(identity) for identity in status.accepted_item_ids]),
            payload,
        )
        with self._connection:
            row = self._connection.execute(
                "SELECT payload_json FROM source_statuses WHERE source = ?", (status.source,)
            ).fetchone()
            if row is not None:
                if row["payload_json"] != payload:
                    self._connection.execute(
                        """
                        UPDATE source_statuses
                        SET adapter_id=?, cursor_value=?, item_count=?, last_attempted_at=?, last_success_at=?,
                            last_request_id=?, last_receipt_request_id=?, error_class=?, retry_after_seconds=?,
                            accepted_item_ids_json=?, payload_json=?
                        WHERE source=?
                        """,
                        (*values[1:], status.source),
                    )
                    return True
                return False
            self._connection.execute(
                """
                INSERT INTO source_statuses(
                    source, adapter_id, cursor_value, item_count, last_attempted_at, last_success_at,
                    last_request_id, last_receipt_request_id, error_class, retry_after_seconds,
                    accepted_item_ids_json, payload_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                values,
            )
            return True

    def record_poll_receipt(self, receipt: PollReceipt) -> bool:
        payload = self._encode(self._poll_receipt_payload(receipt))
        values = (
            receipt.source,
            receipt.adapter_id,
            receipt.request_id,
            receipt.accepted_cursor.value if receipt.accepted_cursor else None,
            self._encode([self._identity_payload(identity) for identity in receipt.accepted_item_ids]),
            receipt.item_count,
            receipt.error_class,
            receipt.retry_after_seconds,
            payload,
        )
        with self._connection:
            row = self._connection.execute(
                "SELECT payload_json FROM poll_receipts WHERE source=? AND adapter_id=? AND request_id=?",
                (receipt.source, receipt.adapter_id, receipt.request_id),
            ).fetchone()
            if row is not None:
                if row["payload_json"] != payload:
                    raise ItemConflictError("conflicting poll receipt")
                return False
            self._connection.execute(
                """
                INSERT INTO poll_receipts(
                    source, adapter_id, request_id, accepted_cursor_value, accepted_item_ids_json,
                    item_count, error_class, retry_after_seconds, payload_json
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                values,
            )
            return True

    def search(self, query: str, *, limit: int = 20) -> list[InboxItem]:
        text = query.strip()
        if not text or not 1 <= limit <= 100:
            raise ValueError("query and limit must be valid")
        like = f"%{text}%"
        rows = self._connection.execute(
            """
            SELECT * FROM items
            WHERE title LIKE ? COLLATE NOCASE OR body LIKE ? COLLATE NOCASE
            ORDER BY rowid DESC LIMIT ?
            """,
            (like, like, limit),
        ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def recent(self, *, limit: int = 20) -> list[InboxItem]:
        """Return the newest stored item previews without interpreting priority."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        rows = self._connection.execute(
            "SELECT * FROM items ORDER BY rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def get(self, identity: ItemIdentity) -> InboxItem | None:
        row = self._connection.execute(
            "SELECT * FROM items WHERE source = ? AND item_id = ?",
            (identity.source, identity.item_id),
        ).fetchone()
        return None if row is None else self._row_to_item(row)

    def record_receipt(self, receipt: ActionReceipt) -> bool:
        if not receipt.accepted or receipt.receipt_id is None:
            raise ValueError("only accepted receipts are durable")
        payload = self._encode(
            {
                "accepted": receipt.accepted,
                "outcome": receipt.outcome,
                "source": receipt.action.identity.source,
                "item_id": receipt.action.identity.item_id,
                "kind": receipt.action.kind,
            }
        )
        values = (
            receipt.action.identity.source,
            receipt.action.identity.item_id,
            receipt.action.kind,
            receipt.receipt_id,
        )
        with self._connection:
            row = self._connection.execute(
                "SELECT payload_json FROM action_receipts WHERE source=? AND item_id=? AND action_kind=? AND receipt_id=?",
                values,
            ).fetchone()
            if row is not None:
                if row["payload_json"] != payload:
                    raise ItemConflictError("conflicting action receipt")
                return False
            self._connection.execute(
                "INSERT INTO action_receipts(source,item_id,action_kind,receipt_id,payload_json) VALUES (?,?,?,?,?)",
                (*values, payload),
            )
            return True

    def _upsert_cursor(self, cursor: InboxCursor | None) -> None:
        if cursor is None or cursor.source is None:
            return
        self._connection.execute(
            "INSERT INTO source_cursors(source,cursor_value) VALUES (?,?) ON CONFLICT(source) DO UPDATE SET cursor_value=excluded.cursor_value",
            (cursor.source, cursor.value),
        )

    def _row_to_item(self, row: sqlite3.Row) -> InboxItem:
        identity = ItemIdentity(source=row["source"], item_id=row["item_id"])
        refs = tuple(ItemRef(identity=identity, label=entry["label"]) for entry in json.loads(row["refs_json"]))
        cursor = InboxCursor(row["cursor_value"], source=identity.source) if row["cursor_value"] else None
        payload = json.loads(row["payload_json"])
        return InboxItem(
            identity=identity,
            title=row["title"],
            body=row["body"],
            sender=payload.get("sender"),
            refs=refs,
            cursor=cursor,
            content_state=ContentState(row["content_state"]),
        )

    def _row_to_source_status(self, row: sqlite3.Row) -> SourceStatus:
        cursor = InboxCursor(row["cursor_value"], source=row["source"]) if row["cursor_value"] else None
        accepted_item_ids = tuple(
            ItemIdentity(source=entry["source"], item_id=entry["item_id"])
            for entry in json.loads(row["accepted_item_ids_json"])
        )
        return SourceStatus(
            source=row["source"],
            adapter_id=row["adapter_id"],
            cursor=cursor,
            item_count=row["item_count"],
            last_attempted_at=row["last_attempted_at"],
            last_success_at=row["last_success_at"],
            last_request_id=row["last_request_id"],
            last_receipt_request_id=row["last_receipt_request_id"],
            error_class=row["error_class"],
            retry_after_seconds=row["retry_after_seconds"],
            accepted_item_ids=accepted_item_ids,
        )

    def _row_to_poll_receipt(self, row: sqlite3.Row) -> PollReceipt:
        accepted_cursor = (
            InboxCursor(row["accepted_cursor_value"], source=row["source"])
            if row["accepted_cursor_value"]
            else None
        )
        accepted_item_ids = tuple(
            ItemIdentity(source=entry["source"], item_id=entry["item_id"])
            for entry in json.loads(row["accepted_item_ids_json"])
        )
        return PollReceipt(
            source=row["source"],
            adapter_id=row["adapter_id"],
            request_id=row["request_id"],
            accepted_item_ids=accepted_item_ids,
            accepted_cursor=accepted_cursor,
            item_count=row["item_count"],
            error_class=row["error_class"],
            retry_after_seconds=row["retry_after_seconds"],
        )

    @staticmethod
    def _item_payload(item: InboxItem) -> dict[str, object]:
        return {
            "source": item.identity.source,
            "item_id": item.identity.item_id,
            "title": item.title,
            "body": item.body,
            "sender": item.sender,
            "content_state": item.content_state.value,
            "refs": [{"label": ref.label} for ref in item.refs],
        }

    @staticmethod
    def _identity_payload(identity: ItemIdentity) -> dict[str, str]:
        return {"source": identity.source, "item_id": identity.item_id}

    @staticmethod
    def _source_status_payload(status: SourceStatus) -> dict[str, object]:
        return {
            "source": status.source,
            "adapter_id": status.adapter_id,
            "cursor": status.cursor.value if status.cursor else None,
            "item_count": status.item_count,
            "last_attempted_at": status.last_attempted_at,
            "last_success_at": status.last_success_at,
            "last_request_id": status.last_request_id,
            "last_receipt_request_id": status.last_receipt_request_id,
            "error_class": status.error_class,
            "retry_after_seconds": status.retry_after_seconds,
            "accepted_item_ids": [SQLiteInboxStore._identity_payload(identity) for identity in status.accepted_item_ids],
        }

    @staticmethod
    def _poll_receipt_payload(receipt: PollReceipt) -> dict[str, object]:
        return {
            "source": receipt.source,
            "adapter_id": receipt.adapter_id,
            "request_id": receipt.request_id,
            "accepted_cursor": receipt.accepted_cursor.value if receipt.accepted_cursor else None,
            "accepted_item_ids": [SQLiteInboxStore._identity_payload(identity) for identity in receipt.accepted_item_ids],
            "item_count": receipt.item_count,
            "error_class": receipt.error_class,
            "retry_after_seconds": receipt.retry_after_seconds,
        }

    @staticmethod
    def _encode(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _count(self, table: str) -> int:
        return int(self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
