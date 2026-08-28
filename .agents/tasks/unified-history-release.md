# Unified history release

Started at 2026-08-28T17:32:24+03:00 (manual clock)
Estimate: 30-90 active minutes; active time is not continuously measured.

Status: complete review and automated release proof; integration commit pending

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
