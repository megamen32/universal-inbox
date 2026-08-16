# Universal Inbox ↔ Universal Outbox router

- Status: active
- Accepted result: one hub-and-spoke platform contract; no provider-pair adapters.
- Shortest real canary: a Telegram text update is persisted by Universal Inbox,
  routed by a scoped NoticePlace consumer to Matrix, then a Matrix text event is
  persisted and routed by another scoped consumer to Telegram, with provider
  receipts and no echo loop.
- Current proof: local Inbox suite 71 passed after the Bot API and webhook
  slices; scoped Outbox suite 247 passed with 0 failures/errors/skips (the one
  unrelated Agent Herder symbol test remains excluded). Focused generic
  Telegram/Matrix delivery and Telegram interaction tests: 64 passed.
- Current production path: `notification-center.service` is active from
  `/opt/noticeplace`; Telegram Bot API credentials and chat exist. Its existing
  callback poller is the single Telegram update owner and now has an optional
  canonical Universal Inbox sink. Hermes Matrix credentials are valid but the
  account currently reports zero joined rooms.
- Published revisions: Universal Inbox `4f755cf` adds immutable-release
  installer plus loopback webhook and Matrix systemd units; NoticePlace
  `29224c4` registers generic Telegram message delivery.
- Blockers to full bidirectional proof: rotate the exposed MatrixRTC bridge
  credential with its counterpart owner; deploy task-owned revisions; install
  one Inbox service on loopback port 18092 and scoped consumers; obtain/join an
  exact Matrix room for the two-message canary. The callback-actor and
  message-sender allowlists are now separate. SSS inventory timed out after
  300 seconds.
- Safety: preserve foreign dirty files; never print provider tokens; loop guard
  filters own/bot senders and exact allowlisted chats/rooms; deploy/restart and
  external sends must be reported as consequential boundaries.
- Next action: independent Critic review of the exact Telegram canonical-ingress
  slice, then managed deploy and one controlled Telegram→Matrix canary if an
  operator-owned Matrix room is available.
