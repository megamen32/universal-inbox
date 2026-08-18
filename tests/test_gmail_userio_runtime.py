from __future__ import annotations

from universal_inbox.adapters._read_only import ReadOnlyPage
from universal_inbox.adapters.gmail import GmailReadAdapter
from universal_inbox.gmail_userio_runtime import build_gmail_userio_watches
from universal_inbox.registry import AdapterRegistry
from universal_inbox.store import SQLiteInboxStore


def test_build_gmail_userio_watches_keeps_provider_credentials_outside_runtime(monkeypatch, tmp_path) -> None:
    adapter = GmailReadAdapter(
        adapter_id="gmail-personal",
        source="gmail",
        reader=lambda _cursor, _limit: ReadOnlyPage(items=(), next_cursor=None),
    )
    monkeypatch.setattr("universal_inbox.gmail_userio_runtime._default_registry", lambda: AdapterRegistry((adapter,)))
    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        watches = build_gmail_userio_watches(
            store,
            {
                "UNIVERSAL_USERIO_INGRESS_URL": "http://127.0.0.1:18093",
                "UNIVERSAL_USERIO_INGRESS_TOKEN": "opaque-token",
            },
        )
    assert len(watches) == 1


def test_build_gmail_userio_watches_requires_an_opaque_userio_token(tmp_path) -> None:
    with SQLiteInboxStore(tmp_path / "inbox.sqlite3") as store:
        try:
            build_gmail_userio_watches(store, {})
        except RuntimeError as error:
            assert "TOKEN" in str(error)
        else:
            raise AssertionError("expected opaque token requirement")
