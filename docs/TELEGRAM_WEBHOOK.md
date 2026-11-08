# Telegram webhook deployment

Endpoint: `POST https://<deployment-host>/telegram/webhook/` (include the trailing slash).
The endpoint accepts UTF-8 JSON updates up to 64 KiB. It is CSRF-exempt because it
authenticates Telegram with a separate secret, not a browser session.

## Configuration

Set these server environment variables without exposing them in client code or logs:

- `TELEGRAM_BOT_TOKEN`: the existing bot token.
- `TELEGRAM_BOT_USERNAME`: the existing bot username, without `@`.
- `TELEGRAM_WEBHOOK_SECRET`: a randomly generated secret (recommend 32 random
  bytes encoded as hexadecimal). Telegram allows 1–256 characters from
  `A-Z`, `a-z`, `0-9`, `_`, `-`.
- `TOURNAMENT_DEMO_MODE=1` for the public demo. Telegram identity login remains
  enabled, while financial callbacks and financial notification delivery remain disabled.
- Keep the designated cashier configuration unchanged. Webhook delivery grants
  no user a cashier or staff role.

After deployment and migrations succeed, stop the local polling process before
activating webhook delivery. Do not run polling and webhook handling together.
Configure the official Bot API `setWebhook` method with:

| Parameter | Value |
| --- | --- |
| `url` | The HTTPS endpoint above |
| `secret_token` | Exactly the server's `TELEGRAM_WEBHOOK_SECRET` |
| `allowed_updates` | `["message", "callback_query"]` |
| `max_connections` | `1` |
| `drop_pending_updates` | `false` (preserve undelivered user actions) |

Telegram then attaches the header
`X-Telegram-Bot-Api-Secret-Token: <configured-secret>` to every update. Missing or
incorrect secrets return 403. Unconfigured/invalid server secrets return 503.
Registering the webhook itself sends no chat messages; subsequent delivery of
pending or newly initiated user actions can produce the normal bot responses.
Do not create synthetic login commands or test payments against the live bot.
Inspect `getWebhookInfo` for successful configuration and delivery errors without
printing bot-token URLs or secrets. A real login test must be initiated by the
user from their browser.

## Reliability and limits

Receipts are stored per `update_id` in `BotState`; a smaller out-of-order ID is
not discarded. A completed duplicate returns 200 without repeating processing.
A global database lease serializes bot updates across Vercel invocations. Its
claim/finalization transactions are short; Telegram HTTP runs outside them.
Busy or transient failures return 503 with `Retry-After: 5`, allowing Telegram to
retry. A terminated invocation's lease expires after 120 seconds. A client call
requires at least 30 seconds remaining (the transport timeout is 25 seconds).
An old invocation cannot clear a newer owner's lease. Permanent `sendMessage`
400/403 failures are acknowledged so a blocked user cannot poison delivery.

Outgoing Telegram messages are **at least once**: an ambiguous timeout can cause
a duplicate message on retry. Payment decisions retain transactional status and
bank-reference checks. Incorrect login-code attempts are counted once per update,
even if sending the denial fails. Receipts store no incoming body, code or secret.
Receipt cleanup is not automatic; plan retention/maintenance if this becomes a
long-lived high-volume service.

The webhook does **not** flush the financial outbox. The demo must keep it disabled.
Before accepting real fees on serverless hosting, provide a separate scheduled
outbox worker with authenticated invocation, retry monitoring and concurrency
controls. Do not rely on an unrelated incoming Telegram update to send a receipt.

Local tests mock all Bot API calls. The PostgreSQL multi-worker test runs only
against a PostgreSQL test database; SQLite runs skip that one check.

Primary reference: [Telegram Bot API — setWebhook and Update](https://core.telegram.org/bots/api#setwebhook).
Telegram documents retry on non-2xx responses, secret-token headers, concurrent
delivery limits, update IDs for deduplication/out-of-order delivery, and the mutual
exclusion of polling and webhook delivery.
