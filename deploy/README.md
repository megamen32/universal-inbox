# Universal Inbox deployment

The two services are a single ingress deployment: the webhook owns provider
bridge input (including the existing NoticePlace-owned Telegram Bot API poller)
and the Matrix worker owns one allowlisted `/sync` cursor. They share the same
durable Inbox database and source-to-NoticePlace consumer routes.

```bash
sudo ./deploy/universal-inbox install --no-start
sudoedit /etc/universal-inbox.env
sudo ./deploy/universal-inbox upgrade
```

The deploy command archives the committed `HEAD` into an immutable release and
keeps `/etc/universal-inbox.env` and `/var/lib/universal-inbox` intact. Do not
start a second Telegram Bot API reader: NoticePlace remains the only
`getUpdates` owner and posts canonical envelopes to the loopback webhook.
