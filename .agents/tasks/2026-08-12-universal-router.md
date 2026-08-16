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
- Live proof (2026-08-17): `universal-inbox-webhook.service` and
  `universal-inbox-matrix.service` are active; loopback ingress listens on
  `127.0.0.1:18092`. Matrix event
  `$8-_Le4uOSVGVohAn6lYlRbE2rC_YOvwS8r_EwSb_UGM` reached Telegram as receipt
  `message_id=9950`. Telegram message `540308572:9951` was ingested and
  delivered to Matrix as event
  `$DiAsy7UGKawEXmOxan6a8bZ3dJyX051PmSwm0DDCz9U`; the following Matrix poll did
  not ingest that outbound event, proving the self-sender loop guard.
- MatrixRTC credential rotation remains intentionally deferred by user
  direction. The callback-actor and message-sender allowlists are separate.
- Safety: preserve foreign dirty files; never print provider tokens; loop guard
  filters own/bot senders and exact allowlisted chats/rooms; deploy/restart and
  external sends must be reported as consequential boundaries.
- Next action: independent Critic review of the exact Telegram canonical-ingress
  slice, then managed deploy and one controlled Telegram→Matrix canary if an
  operator-owned Matrix room is available.
