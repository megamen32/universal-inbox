# Universal Inbox → Universal Outbox routing

The integration is hub-and-spoke. Every provider enters Universal Inbox once,
and every destination is implemented once in NoticePlace. Source-specific
NoticePlace consumer tokens select operator-owned delivery policies; producers
never submit provider URLs, credentials, or destination targets.

## Ingress

- Telegram has two plugin seams. A bot-exclusive deployment can run
  `python -m universal_inbox.noticeplace_runtime --source telegram` with
  `UNIVERSAL_INBOX_TELEGRAM_TRANSPORT=bot-api`; a user-session deployment uses
  the existing Overpod Unix socket. When NoticePlace already owns that bot's
  `getUpdates` cursor, its in-process poller must be the single reader and POST
  canonical messages to the webhook ingress instead of starting a second
  Bot API poller. The standalone Bot API runtime additionally requires
  `UNIVERSAL_INBOX_TELEGRAM_SINGLE_READER=true` as an explicit ownership gate.
- Matrix: `python -m universal_inbox.noticeplace_runtime --source matrix`
  reads allowlisted rooms through Matrix `/sync`. The first sync records only a
  baseline cursor, so installation never forwards historical timeline events.
- Both commands poll continuously by default; `--once` is reserved for bounded
  operator canaries and timer-driven deployments.
- Telegram/Matrix shared runtime bridges, WhatsApp, VK, and phone transcription
  bridges: POST
  `universal.inbox.message.v1` to the loopback/LAN-restricted
  `python -m universal_inbox.webhook_runtime` endpoint.

Example envelope:

```json
{
  "schema": "universal.inbox.message.v1",
  "source": "whatsapp",
  "message_id": "provider-stable-message-id",
  "sender": "provider-stable-sender-id",
  "body": "message text"
}
```

## Routing

`UNIVERSAL_INBOX_NOTICEPLACE_ROUTES_JSON` maps one source to one scoped
NoticePlace consumer token. The consumer's durable policy may contain any
supported sequence of:

- `telegram.message`
- `matrix.message`
- `whatsapp.message`
- `vk.message`
- `telegram.call`
- `matrix.call`
- `whatsapp.call`
- `phone.call`

Example shape (placeholders only; the real file must be root/user-owned `0600`):

```text
UNIVERSAL_INBOX_NOTICEPLACE_EVENT_URL=http://127.0.0.1:8091/v1/events
UNIVERSAL_INBOX_NOTICEPLACE_ROUTES_JSON={"telegram":{"token":"replace-with-telegram-source-consumer-token","recipient":"matrix-route"},"matrix":{"token":"replace-with-matrix-source-consumer-token","recipient":"telegram-route"},"whatsapp":{"token":"replace-with-whatsapp-source-consumer-token","recipient":"operator"},"vk":{"token":"replace-with-vk-source-consumer-token","recipient":"operator"},"phone":{"token":"replace-with-phone-source-consumer-token","recipient":"operator"}}
```

NoticePlace owns destination credentials:

```text
MATRIX_MESSAGE_HOMESERVER=https://matrix.example
MATRIX_MESSAGE_ACCESS_TOKEN=replace-with-dedicated-token
MATRIX_MESSAGE_ROOM_ID=!operator-room:matrix.example
NOTIFY_MESSAGE_WEBHOOKS_JSON={"whatsapp.message":{"url":"http://127.0.0.1:18110/v1/messages","token":"replace-with-bridge-token"},"vk.message":{"url":"http://127.0.0.1:18111/v1/messages","token":"replace-with-bridge-token"}}
NOTIFY_CALL_WEBHOOKS_JSON={"phone.call":{"url":"http://127.0.0.1:18112/v1/calls","token":"replace-with-bridge-token"},"telegram.call":{"url":"http://127.0.0.1:18113/v1/calls","token":"replace-with-bridge-token"},"whatsapp.call":{"url":"http://127.0.0.1:18110/v1/calls","token":"replace-with-bridge-token"}}
```

## Universal UserIO fan-out

When `UNIVERSAL_USERIO_INGRESS_URL` is configured, the same durable canonical
item is also POSTed to UserIO's `/v1/messages` endpoint. The source cursor
advances only after both NoticePlace and UserIO accept the item; each service
deduplicates using the stable Inbox identity. `UNIVERSAL_USERIO_ROUTES_JSON`
maps the source to a UserIO `route_id`; it contains no provider URL or
credential. The UserIO ingress token is scoped only to message ingestion.

```text
UNIVERSAL_USERIO_INGRESS_URL=http://127.0.0.1:18093
UNIVERSAL_USERIO_INGRESS_TOKEN=replace-with-userio-ingress-token
UNIVERSAL_USERIO_ROUTES_JSON={"telegram":"telegram-reply","matrix":"matrix-reply","whatsapp":"whatsapp-reply","vk":"vk-reply","phone":"phone-reply"}
```

UserIO owns the conversation, AI draft and approval decision. Its later
`userio.reply.v1` intent enters NoticePlace through a scoped producer route;
Universal Inbox is never asked to make an AI or delivery decision.

## Loop and retry safety

- Telegram ignores the configured own user plus
  `UNIVERSAL_INBOX_TELEGRAM_IGNORED_SENDER_IDS` (including the Outbox bot).
- Matrix ignores `UNIVERSAL_INBOX_MATRIX_USER_ID`.
- Webhook bridges ignore sender IDs from
  `UNIVERSAL_INBOX_WEBHOOK_IGNORED_SENDERS_JSON`, for example
  `{"whatsapp":["outbox-account-id"],"vk":["relay-user-id"]}`.
- Inbox refs and NoticePlace events use stable idempotency keys.
- Matrix sends use a stable transaction ID. Message/call webhook bridges must
  honor the supplied `Idempotency-Key` and return a stable `receipt_id`.
- All calls, including generic provider calls, obey the NoticePlace
  `automatic_calls_enabled` kill switch.

## Deployment boundary

Source and local canaries do not deploy this integration. A real rollout still
requires scoped NoticePlace consumers, provider credentials and exact
allowlists/ignored sender IDs, installation of the relevant ingress processes
on the hosts that own those sessions, a NoticePlace managed upgrade, and a
controlled real Telegram ↔ Matrix canary.
