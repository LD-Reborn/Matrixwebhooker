# MatrixWebhooker

A tiny webhook → Matrix bridge with support for **multiple bot accounts**.
Send an alert to `POST /webhook`, and it shows up in a Matrix room as a
message from the bot identified by the API key you used:

```
Grafana, CI, ... ──HTTP──▶ MatrixWebhooker ──Matrix API──▶ per-bot Matrix room
                              (one entry per bot)
```

## Setup

1. Copy and fill in the config files:

   ```sh
   cp .env.example .env          # shared settings (no secrets)
   cp bots.example.json bots.json   # bot roster (secrets — stays out of git)
   ```

   - `.env` — `MATRIX_HOME_SERVER`, `MATRIX_INVITE_USER` (invited into
     auto-created rooms), `HOST`/`PORT`.
   - `bots.json` — **one entry per bot account**:

     ```json
     {
       "bots": [
         {
           "name": "grafana_bot",
           "user": "grafana_bot",
           "password": "...",
           "api_key": "...",
           "room_id": null,
           "room_name": null
         }
       ]
     }
     ```

     - `api_key` — the secret this bot must present on `/webhook`
       (`X-API-Key` or `Authorization: Bearer`). One per bot, all unique.
       Generate: `python3 -c 'import secrets; print(secrets.token_urlsafe(24))'`
     - `room_id` — pinned target room. `null` (default): the bot uses its
       remembered room, or creates one (`room_name` or "<Bot Name> Alerts")
       and invites `MATRIX_INVITE_USER` — the room ID is then cached,
       never re-created.
     - Adding a bot = registered Matrix account + one new entry. Restart to
       pick it up.

2. Run:

   ```sh
   pip install -r requirements.txt
   python3 server.py
   ```

   **Or with Docker (recommended for always-on use):**

   ```sh
   docker compose up -d --build
   docker compose logs -f        # follow logs
   docker compose down           # stop (state in the named volume is kept)
   ```

   The container runs as an unprivileged user and `restart: unless-stopped`
   brings it back after crashes *and* host reboots — until you explicitly
   stop it. Per-bot access tokens and remembered room IDs live in the
   `matrix-webhooker-state` volume, so restarts don't re-login (the server
   rate-limits logins hard) and don't re-create rooms.
   `.env` and `bots.json` are bind-mounted read-only, never baked in.
   Login is **lazy**: a bot logs in on its first use, so starting the
   container never triggers a login storm.

   > Tip: for a homeserver on the *same* host (e.g. Synapse in Docker with
   > port 8008), you may prefer
   > `MATRIX_HOME_SERVER=http://host.docker.internal:8008` to stay on the
   > LAN instead of public DNS.

## API

Auth header for every request: `X-API-Key: <api_key of the bot>` (or
`Authorization: Bearer <key>`).

### `POST /webhook` — send a message

```sh
curl -X POST http://localhost:8080/webhook \
  -H "X-API-Key: $GRAFANA_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "High CPU on web-1"}'
```

| field     | required | description                                              |
|-----------|----------|----------------------------------------------------------|
| `text`    | yes      | plain-text message (alias: `msg`)                        |
| `msgtype` | no       | `m.text` (default) or `m.notice`                         |
| `html`    | no       | optional HTML body (rendered as `formatted_body`)        |
| `room_id` | no       | send to another room instead of the bot's default        |

Response: `{"ok": true, "bot": "grafana_bot", "room": "!...:domain", "event_id": "$..."}`

### `GET /me` — which bot is this key?

With an API key: `{"bot": "grafana_bot", "user": "@grafana_bot:...", "room": "..."}`.
Useful to check which key you're holding.

### `GET /rooms` — rooms the bot is in

With the bot's API key: `{"bot": "...", "rooms": [{"room_id": "!...:domain", "name": "..."}]}`.

### `GET /rooms/<room_id>/messages` — read a room

Most recent room messages (default `?limit=50`, max `100`), oldest first:

```json
{"bot": "grafana_bot", "room": "!...:domain", "messages": [
  {"event_id": "$...", "sender": "@grafana_bot:...", "msgtype": "m.text",
   "body": "High CPU on web-1", "ts": 1790723107085}
]}
```

### `GET /health` — no auth

Service status plus per-bot state (`connected`, `room`).

## MCP server (optional)

The same bot model is exposed as an MCP server (`mcp_server.py`) with four
tools — `list_bots`, `list_rooms`, `read_messages`, `send_message` — so an
AI assistant can read from and send to your Matrix rooms. It talks to the
homeserver **directly**, so the HTTP server does not need to be running.
Same config (`.env` + `bots.json`); optional `MCP_BOT` env sets the default
bot for tool calls without a `bot` argument.

```sh
pip install -r requirements-mcp.txt     # adds the 'mcp' package
python3 mcp_server.py                   # stdio MCP server (for manual inspection)
```

Register with OpenCode (global = available in every project):

```sh
opencode mcp add matrix --global -- python3 /path/to/MatrixWebhooker/mcp_server.py
opencode mcp list                       # ✓ matrix  connected
```

After that, in any session you can just ask: *“list the rooms the grafana bot
is in”*, *“read the last 20 messages of the Grafana Alerts room”*, *“tell
webhook_bot that the deploy is done”*. Generic MCP client config (Claude
Desktop & friends):

```json
{ "mcpServers": { "matrix": {
    "command": "python3",
    "args": ["/path/to/MatrixWebhooker/mcp_server.py"] } } }
```

## Notes & limitations

- Plaintext rooms only — no end-to-end encryption.
- If a bot isn't in the room but has a pending invitation, it auto-joins.
- Access tokens are cached per bot (state volume/file); on a 401 the bot
  re-logs in and retries once.
- A bot that runs from more than one place (container + MCP server) caches a
  separate token/login per place — that's one Matrix device each, both stay
  valid in parallel.
