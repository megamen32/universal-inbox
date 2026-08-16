# Universal Inbox ↔ Universal Outbox router

- Status: active
- Accepted result: one hub-and-spoke platform contract; no provider-pair adapters.
- Shortest real canary: a Telegram text update is persisted by Universal Inbox,
  routed by a scoped NoticePlace consumer to Matrix, then a Matrix text event is
  persisted and routed by another scoped consumer to Telegram, with provider
  receipts and no echo loop.
- Current proof: local Inbox suite 71 passed after the Bot API and webhook
  slices; Outbox focused Telegram interaction suite 7 passed. A full scoped
  Outbox run was started after the durable-offset fix, but its terminal receipt
  needs to be recorded before release.
- Current production path: `notification-center.service` is active from
  `/opt/noticeplace`; Telegram Bot API credentials and chat exist. Its existing
  callback poller is the single Telegram update owner and now has an optional
  canonical Universal Inbox sink. Hermes Matrix credentials are valid but the
  account currently reports zero joined rooms.
- Blockers to full bidirectional proof: deploy the task-owned Outbox source;
  install one Inbox service on loopback port 18092 and scoped consumers;
  obtain/join an exact Matrix room for the two-message canary. The
  callback-actor and message-sender allowlists are now separate. SSS inventory
  timed out after 300 seconds.
- Safety: preserve foreign dirty files; never print provider tokens; loop guard
  filters own/bot senders and exact allowlisted chats/rooms; deploy/restart and
  external sends must be reported as consequential boundaries.
- Next action: independent Critic review of the exact Telegram canonical-ingress
  slice, then managed deploy and one controlled Telegram→Matrix canary if an
  operator-owned Matrix room is available.
