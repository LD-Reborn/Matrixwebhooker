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

## Triggers (incoming messages)

Beyond pushing alerts *into* Matrix, a bot can **react to messages it
receives**. Add `triggers` to a bot entry in `bots.json`:

```json
{
  "bots": [
    {
      "name": "grafana_bot",
      "user": "grafana_bot",
      "password": "...",
      "api_key": "...",
      "room_id": "!...:domain",
      "triggers": [
        {
          "room": "Grafana Bot Alerts",
          "command": "opencode run --standalone -m ollama/qwen3.8:27b-mtp-q4_K_M-128k \"You got a new message in matrix channel {{channel}}: {{msg}}\"",
          "context": 25
        },
        { "room": "!...:domain", "command": ["curl -fsS https://example.local/hook", "logger -t mw 'triggered'"] }
      ]
    }
  ]
}
```

- `room` — the channel to listen in: the room's **display name**
  (case-insensitive) or a **room id** (`!…`). Use a room id for DMs (they
  have no display name). One bot can have many triggers.
- `command` — one **bash** command line, or a list of them run in sequence
  in the app directory. Placeholders are substituted (and shell-quoted, so
  no injection/quoting worries):
  - `{{channel}}` — the room's display name (or id if it has none)
  - `{{msg}}` — the last `context` messages of the room, one per line,
    newest last, e.g. `[2026-10-04 12:01:22] @user: deploy done`
  - `{{sender}}` — the sender of the new message
  - `{{room}}` — the room id
- `context` — how many messages `{{msg}}` holds (default `25`, max `100`).
- A trigger fires only for a message **from someone else** — messages the
  bot itself sent are always skipped (no loops).
- If a client is **waiting** for that room at the moment (see below), the
  message goes to the waiting client *instead of* the triggers.
- Runs in a background thread; long commands (like a full `opencode run`)
  don't block the watcher. Restart the server after editing `bots.json` to
  load changes. In the slim Docker image there is no `bash`, so commands
  fall back to `sh` — keep them POSIX-safe if you run Docker.

### Waiting for a reply

Ask a question and block for the answer instead of firing a trigger:

- **MCP tool** `wait_for_reply(room, bot="", timeout=120)` — blocks up to
  `timeout` seconds (max 600) for the next message in the room that is not
  from the bot itself: `{"received": {"sender", "body", "ts", ...}}`, or
  `{"received": null}` on timeout. Natural pattern for a conversational
  trigger: the spawned `opencode run` can `send_message` a clarifying
  question, then `wait_for_reply` for the human's answer — and that answer
  is handed to it directly, never re-triggering the bot.
- **HTTP** `POST /wait` with `{"room_id": "!…", "timeout": 120}` — same
  semantics, auth with the bot's API key (names the bot that waits).

Both are served by the long-lived `server.py` (which owns the watcher and
the trigger/wait registry), so `wait_for_reply` needs `server.py` running —
the other MCP tools work without it. `MATRIX_WEBHOOKER_URL` points the tool
at the server (default `http://localhost:8080`).

### Wrench feedback (🔧)

While the bot is **handling** a message it reacts to it with the wrench
emoji, and removes the reaction again as soon as that handling is done:

- a message that **fires a trigger** gets 🔧 as soon as the command starts;
- a message **delivered to a model that is waiting** (`wait_for_reply`) also
  gets 🔧 while the run it belongs to is still in flight.

The wrench is removed (the reaction event is redacted) when the trigger
command(s) finish, the run is stopped, or the room's pending wait is
released — so in the room you can always tell which messages the bot is
still working on.

### Stopping a run

To interrupt the current execution in a room, just send a bare **`stop`**
(any capitalization, optional leading `/`, trailing punctuation is fine —
`stop`, `/stop`, `STOP`, `STOP!!!`). It contains no whitespace, which is the
whole rule: a sentence that merely starts with "stop" such as
`stop ollama.service` is a normal message, *not* a stop. On `stop` the bot:

1. kills the running trigger command (SIGTERM the process group, then SIGKILL);
2. releases any model blocked in `wait_for_reply` for that room
   (`POST /wait` returns `{"stopped": true, "received": null}`);
3. removes every wrench reaction it added for that room;
4. replies with a short confirmation (`⏹ Stopped.` / `⏹ Stop sent …`).

> **`opencode run` triggers need `--standalone`.** Without it, `opencode run`
> is only a thin client: it submits the job to OpenCode's *shared background
> service* (`opencode serve --service`) and streams the output. `stop` kills
> the client's process group and the client dies (`exit -15`), but the shared
> service keeps the model turn running and will **still post the answer** —
> and killing the shared service outright would also kill your IDE's session.
> With `--standalone`, the run executes in its own process tree, so the
> process-group kill actually stops it. If you use any other long-lived tool
> as a trigger, make sure it runs the work in-process (not in a shared
> daemon) or `stop` has the same problem.

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

The same bot model is exposed as an MCP server (`mcp_server.py`) with six
tools — `list_bots`, `list_rooms`, `read_messages`, `send_message`, `react`,
`wait_for_reply` — so an
AI assistant can read from and send to your Matrix rooms. `react(event_id,
key, room)` adds an emoji reaction to a specific message (e.g. `key="✅"`),
using the `event_id` returned by `read_messages`. It talks to the
homeserver **directly**, so the HTTP server does not need to be running —
*except* `wait_for_reply`, which blocks on a message and is served by
`server.py` (which owns the triggers); see "Waiting for a reply" above.
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
