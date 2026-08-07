# Universal Inbox Core

> A consumer-neutral local core for normalizing inbox sources, deduplicating items, and exposing a small MCP surface.

Universal Inbox keeps source facts and cursors in durable SQLite storage while leaving credentials, provider sessions, summaries, and consumer routing outside the Core. The current package includes read-only adapter seams for injected Gmail and Telegram Web readers; it does not silently log in, send messages, or perform production cutovers.

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
