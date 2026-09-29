# MatrixWebhooker

A tiny webhook → Matrix bridge. Send an alert to `POST /webhook`, and it shows
up in a Matrix room as a message from your bot account.

```
your alert system ──HTTP──▶ MatrixWebhooker ──Matrix API──▶ [Webhooker Alerts] room
```

## Setup

1. Copy and fill in the env file:

   ```sh
   cp .env.example .env
   ```

   - `MATRIX_HOME_SERVER` / `MATRIX_USER` / `MATRIX_PASSWORD` — bot account
     (on your homeserver, e.g. `@webhook_bot:matrix.yourdomain.tld`).
   - `API_KEY` — secret required on every webhook call. Generate one:
     `python3 -c 'import secrets; print(secrets.token_urlsafe(24))'`
   - `MATRIX_ROOM_ID` — target room. If left empty, the bot creates a private
     room (`MATRIX_ROOM_NAME`) and invites `MATRIX_INVITE_USER`; the room ID is
     saved to `.room_id`.

2. Run:

   ```sh
   python3 server.py          # deps: flask, requests, python-dotenv
   # or: pip install -r requirements.txt && python3 server.py
   ```

## Usage

Send an alert:

```sh
curl -X POST http://localhost:8080/webhook \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "Deploy finished"}'
```

Body fields:

| field     | required | description                                              |
|-----------|----------|----------------------------------------------------------|
| `text`    | yes      | plain-text message (alias: `msg`)                        |
| `msgtype` | no       | `m.text` (default) or `m.notice`                         |
| `html`    | no       | optional HTML body (rendered as `formatted_body`)        |
| `room_id` | no       | send to another room instead of the default              |

`GET /health` reports whether the bot is logged in to the homeserver (no auth).

## Notes & limitations

- Plaintext rooms only — this v1 does not do end-to-end encryption.
- If the bot isn't in the room but has a pending invitation, it auto-joins.
- Access tokens are held in memory only; on a 401 the bot re-logs in and retries.
