# Universal Inbox Core

This project is the receiving side of the loop. For the sending side, delivery,
escalation, calls, and AI-to-human attention routing, see
[NoticePlace / Universal Outbox](https://github.com/megamen32/noticeplace).

> A consumer-neutral local core for normalizing inbox sources, deduplicating items, and exposing a small MCP surface.

Universal Inbox keeps source facts and cursors in durable SQLite storage while leaving credentials, provider sessions, summaries, and consumer routing outside the Core. The package includes read-only adapter seams for Gmail and Telegram MCP readers; it does not silently log in, send messages, or perform production cutovers.

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

The service supports JSON-RPC `initialize`, `tools/list`, and `tools/call` for search, bounded digest candidates, item lookup, adapter manifests, source status, and polling. When the local `himalaya` binary has an allowlisted account configuration, the default registry exposes a read-only Gmail Inbox adapter; otherwise polling remains an intentional successful no-op.

## Verify

```bash
python -m pytest -q
```

## Learn more

- [Development plan](DEVELOPMENT_PLAN.md)
- [Package metadata](pyproject.toml)

The local Gmail adapters use the already-authenticated `himalaya` CLI for the
allowlisted `gmail` and `careviolan` accounts and never write mail. Override the
account list with `UNIVERSAL_INBOX_GMAIL_ACCOUNTS` when needed. Telegram, VK,
WhatsApp, and document integrations remain explicit adapter and authentication
decisions; they are not enabled by this install.

## Telegram secretary wake (opt-in)

`build_configured_telegram_watch` connects an injected read-only Telegram MCP
reader to Agent Herder's `browser_wake` MCP tool. The concrete
`build_overpod_configured_telegram_watch` variant uses the persistent
`@overpod/mcp-telegram` daemon through its Unix socket (normally
`~/.mcp-telegram/daemon.sock`). It calls only `telegram-status`,
`telegram-get-chat-info`, `telegram-get-state`, and `telegram-get-updates`;
Telegram credentials and the GramJS connection stay inside the daemon. The
reader persists the stateless `{pts, qts, date}` cursor in the Universal Inbox
SQLite source cursor and filters updates to the configured DM and group.

Both builders require
`UNIVERSAL_INBOX_AGENT_HERDER_MCP_URL`, separate
`UNIVERSAL_INBOX_TELEGRAM_DM_CHAT_ID` and
`UNIVERSAL_INBOX_TELEGRAM_GROUP_CHAT_ID` values, and a chat-kind preflight that
verifies `dm` and `group`. For the Overpod variant, set
`UNIVERSAL_INBOX_OVERPOD_SOCKET_PATH` only when the daemon uses a non-default
socket, and optionally set `UNIVERSAL_INBOX_TELEGRAM_ACCOUNT_ID` to avoid
discovering the current user ID through the read-only status tool. The Agent
Herder MCP URL is restricted to a loopback host because the bridge cannot prove
that a remote server enforces authentication. It optionally forwards
`UNIVERSAL_INBOX_AGENT_HERDER_MCP_TOKEN` for a locally enforced bearer boundary;
it sends only the durable opaque inbox ref, never the message body. Run the
returned watch with `run_telegram_watch` at the desired polling interval.
Telegram credentials and the provider daemon remain outside this package, so
installing the package does not start polling or send Telegram messages. Start
the Overpod daemon separately under its existing Mac Mini supervisor. Pass an
`on_error` callback to keep the loop alive across transient reader/worker
failures; the durable outbox will retry the undelivered ref on the next poll.

The opt-in process entrypoint is:

```bash
universal-inbox-secretary --db-path ./universal-inbox.sqlite3 --interval-seconds 5
```

Use `--once` for a bounded operator canary. The process requires the configured
Overpod daemon socket, both explicit chat IDs, and the Agent Herder MCP URL; it
does not start the daemon, log in, or send Telegram messages itself.

## Related project

- [NoticePlace / Universal Outbox](https://github.com/megamen32/noticeplace) — sends important AI attention through configured human channels.
