# Unified history release

Started at 2026-08-28T17:32:24+03:00 (manual clock)
Estimate: 30-90 active minutes; active time is not continuously measured.

Status: complete; clean unified history pushed, deployed, and browser-tested

Wanted result: all current reviewed-safe `universal-inbox` work is one clean `main` history, pushed, deployed through the canonical runtime surfaces, and proven by automated plus real-browser tests.
Shortest real canary: authenticated `msg.bezrabotnyi.com` browser journey shows the user dashboard and a working fresh Telegram QR after the deployed unified commit.
Smallest YAGNI vertical slice: review every current diff/untracked path, repair only release-blocking defects, commit all coherent source/docs/tests, run the repository suite, deploy only affected canonical services, then repeat the real browser journey.
Discard now: unrelated product expansion, new abstractions, dependency upgrades not required by the existing diff, and infrastructure redesign.

Release target: `main` in `megamen32/universal-inbox` and the existing `msg.bezrabotnyi.com` deployment.

Review result:

- Absorb the August 10 opt-in Telegram secretary/Agent Herder bridge, CLI, adapter exports, documentation, and tests as one coherent feature.
- Existing tracked store/watch code already contains the prior reviewer-required cursor holdback, durable wake claim/epoch, retry, and completed-only delivery behavior.
- Fixed two remaining release blockers: reject non-loopback Agent Herder MCP endpoints, and reject a `completed` record whose nested receipt is `failed`.
- Removed the empty root `package-lock.json` because the Python project has no root `package.json`; connector packages own their own lockfiles.
- Ignore `.agents/shared-session/` runtime state so time/compaction artifacts cannot dirty release history.

Automated evidence:

- Red proof: focused wake tests failed for both the remote endpoint and record/receipt status mismatch.
- Green proof: focused wake tests `4 passed`; full suite `79 passed`.
- `python -m build --wheel` succeeded and the wheel contains both `universal-inbox` and `universal-inbox-secretary` console scripts.
- `PYTHONPATH=src python -m universal_inbox.secretary_runtime --help`, `python -m compileall -q src tests`, `git diff --check`, and `node --check` for both QR servers succeeded.
- Neither QR package declares an npm test script; no nonexistent package test is claimed.

Activation boundary: the secretary remains deliberately opt-in. The previous selected boundary deferred Telegram/Overpod activation and live Telegram reads/writes; the committed source can be included in the immutable `/opt/universal-inbox` release without starting an unconfigured second Telegram reader.

Live canary blocker after the first deploy:

- The canonical deploy recreated `/var/lib/universal-inbox` as `notification-center:notification-center 0750`, removing traversal for the `roomhacker` Gmail and QR services. The new `account-2` route worked but the QR runtime logged `secure Telegram credential is unavailable`.
- Root cause proof: as `roomhacker`, all three existing age credential files and the Gmail database were unreadable through the parent directory; as `notification-center`, the core `inbox.sqlite3` was not writable because it was owned by `roomhacker`.
- Deployment-contract fix: reapply a scoped `roomhacker:rwx` ACL to the shared state root on every deploy and restore `notification-center` ownership only for the core `inbox.sqlite3*` files.

Release and live evidence:

- Product integration commit `078bfbb` and deployment-contract fix `0b1a95a` were pushed linearly to `origin/main`.
- Canonical immutable deployment points `/opt/universal-inbox` to `/opt/universal-inbox-releases/0b1a95a32b3de131d9337f117821cebc224aec76`.
- State probes passed: `roomhacker` can read/decrypt the existing Telegram QR credentials and write Gmail state; `notification-center` can write the core Inbox database.
- All five Universal Inbox services are active after resetting the WhatsApp start-limit caused by the pre-fix permission loop.
- Managed nginx source already contained the required auth boundary, but live `/etc/nginx` had drifted and removed it. The exact committed vhost was restored with backup, `nginx -t`, and reload; unauthenticated `/`, `/telegram-qr/`, and `/whatsapp-qr/` now redirect to login.
- Authenticated BrowserOS canary logged in as `roomhacker`, confirmed `careviolan@gmail.com` and `megamen932@gmail.com`, and observed Add Gmail/Telegram/VK/WhatsApp controls.
- BrowserOS clicked Add Telegram. The first `account-2` attempt reproduced the state permission error; after the deployment fix, the same prefixed retry reached `account-2 waiting`. Final page content contains both `account-1 waiting` and `account-2 waiting`, each with a Telegram login QR, and no `error`/`Try again` state.
