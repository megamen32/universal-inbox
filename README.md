# Universal Inbox Core

This project is the receiving side of the loop. For the sending side, delivery,
escalation, calls, and AI-to-human attention routing, see
[NoticePlace / Universal Outbox](https://github.com/megamen32/noticeplace).

> A consumer-neutral local core for normalizing inbox sources, deduplicating items, and exposing a small MCP surface.

Universal Inbox keeps source facts and cursors in durable SQLite storage while leaving credentials, provider sessions, summaries, and consumer routing outside the Core. The current package includes read-only adapter seams for injected Gmail and Telegram MCP readers; it does not silently log in, send messages, or perform production cutovers.

Telegram Web/BrowserOS is not a Core adapter. Telegram MCP readers enter through the provider-neutral adapter seam, while any future outbound action must carry durable human evidence: exact payload hash, raw and normalized human response, harness, session, actor, and UTC confirmation time. The affirmative response is exactly `да`; evidence is persisted before a provider write.

## Install

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[test]"
```

## Start in minutes

Start the stdio MCP service:

```bash
universal-inbox --db-path ./inbox.sqlite3
```

The equivalent module entrypoints are:

```bash
python -m universal_inbox --db-path ./inbox.sqlite3
python -m universal_inbox.transport --db-path ./inbox.sqlite3
```

The service supports JSON-RPC `initialize`, `tools/list`, and `tools/call` for search, bounded digest candidates, item lookup, adapter manifests, source status, and polling. With an empty default registry, polling is intentionally a successful no-op.

## Verify

```bash
python -m pytest -q
```

## Learn more

- [Development plan](DEVELOPMENT_PLAN.md)
- [Package metadata](pyproject.toml)

External Gmail, Telegram, VK, WhatsApp, and document integrations remain explicit adapter and authentication decisions; they are not enabled by this install.

## Related project

- [NoticePlace / Universal Outbox](https://github.com/megamen32/noticeplace) — sends important AI attention through configured human channels.
