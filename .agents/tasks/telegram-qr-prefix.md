# Telegram QR prefix routing

Started at 2026-08-28T17:23:18+03:00 (manual clock)
Estimate: 10-30 active minutes; active time not continuously measured.

Status: source fixed and syntax checked; deployment/browser canary pending

Wanted result: the UserIO `Add Telegram account` journey creates and displays a fresh Telegram QR instead of leaving the connector prefix.
Shortest real canary: in a logged-in browser, open `/telegram-qr/`, click `+ Telegram account`, and see a fresh QR under `/telegram-qr/?slot=...`.
Smallest YAGNI slice: render connector actions with the configured `PUBLIC_PREFIX` for both new-account and retry links.
Discard now: Telegram protocol changes, session migration, QR lifetime tuning, 2FA password handling, and unrelated connector refactors.

Red evidence:

- User screenshot shows the failed destination `https://msg.bezrabotnyi.com/new` and `{"error":"not found"}`.
- Production `/telegram-qr/` returns HTML containing `href="/new"` while `/telegram-qr/new` itself correctly redirects to a prefixed slot URL.

Implementation evidence:

- Both new-account and retry links now include configured `PUBLIC_PREFIX`.
- `node --check telegram-qr/server.mjs` passes. A local runtime probe could not start because this checkout intentionally has no installed `telegram` package; the canonical `/opt` deployment has its dependencies.
