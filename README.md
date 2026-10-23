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

   **Or as a systemd service (bare Linux, no Docker):** run the one-time
   deploy script once — it sets up a virtualenv, installs
   `/etc/systemd/system/matrix-webhooker.service`, then enables and starts
   it (same `restart: unless-stopped` semantics as above):

   ```sh
   sudo bash linux_systemd_deploy.sh
   systemctl status matrix-webhooker
   journalctl -u matrix-webhooker -f        # follow logs
   ```

   It runs `server.py` directly as the deploying user with the working
   directory set to the app directory — `.env` and `bots.json` are picked
   up from there. If the `opencode` CLI is installed it *also* writes an
   `opencode.service` unit (headless `opencode serve` on
   `http://127.0.0.1:4096`, password generated into `.env`) — that is what
   trigger/call actions run on.
   The unit's `PATH` puts `~/.local/bin` and `~/.opencode/bin` ahead of
   `/usr/bin`, so tools pick up user-installed versions.
   Re-running the script while the service is running applies the updated
   unit and **restarts** the service (a bare `enable --now` on an already
   running unit would leave the old process in place).
   To pick up `bots.json` / `.env` / code changes: `systemctl restart matrix-webhooker`.
   Uninstall: `systemctl disable --now matrix-webhooker
   && rm -f /etc/systemd/system/matrix-webhooker.service`.

## OpenCode server (what actions run on)

Triggers and calls do not spawn a CLI per message — they talk to one
long-lived headless OpenCode instance:

```sh
OPENCODE_SERVER_PASSWORD=… opencode serve --hostname 127.0.0.1 --port 4096
```

Configure it in `.env`: `OPENCODE_SERVER_URL` (default
`http://127.0.0.1:4096`) and `OPENCODE_SERVER_PASSWORD` (HTTP Basic auth,
user `opencode`; without the env var `opencode serve` prints a random
password at start). The systemd deploy script sets up an `opencode.service`
unit for exactly this and generates the password into `.env`.

Per action the server gets a **fresh session per message**: create
(`POST /api/session` with model/agent/location), attach the system prompt
(instruction entry), send the prompt, poll until the session is idle, take
the final assistant text. Tool use runs headless (the default config asks
no permission questions). Sessions are deleted after a successful run; a
*failed* run keeps its session so you can inspect the turn in the TUI.

## Triggers (incoming messages)

Beyond pushing alerts *into* Matrix, a bot can **react to messages it
receives**. Each trigger runs an **action** on the OpenCode server and
posts the final answer back into the room. Add `triggers` to a bot entry
in `bots.json` — and optionally named **templates** at the top level that
triggers reference (and can override field by field):

```json
{
  "templates": {
    "helper": {
      "model": "ollama/bonsai-2:27b",
      "system": "Du bist ein hilfreicher Assistent. Antworte sachlich auf Deutsch.",
      "timeout": 300
    }
  },
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
          "action": {
            "template": "helper",
            "prompt": "Neue Nachricht in {{channel}} von {{sender}}:\n{{msg}}\nFasse kurz zusammen oder antworte, wenn eine Frage darin steckt."
          },
          "context": 25
        },
        { "room": "!...:domain", "action": { "template": "helper", "model": "ollama/bonsai-2:2b" } }
      ]
    }
  ]
}
```

- `room` — the channel to listen in: the room's **display name**
  (case-insensitive) or a **room id** (`!…`). Use a room id for DMs (they
  have no display name). One bot can have many triggers.
- `templates` — top-level object of named action objects; reference one
  with `"action": {"template": "name"}`, inline keys override it.
- `action` — what runs on the OpenCode server:
  - `prompt` — the user turn; placeholders are inserted **literally**
    (there is no shell any more, so no quoting worries):
    - `{{channel}}` — the room's display name (or id if it has none)
    - `{{msg}}` — the last `context` messages of the room, one per line,
      newest last, e.g. `[2026-10-04 12:01:22] @user: deploy done`
    - `{{sender}}` — the sender of the new message
    - `{{room}}` — the room id
  - `system` — system prompt (attached as a session instruction entry)
  - `model` — `providerID/modelID` (like `opencode run -m`); empty = the
    server's default model
  - `agent` — OpenCode agent id (empty = the default agent)
  - `directory` — where the session runs; default is the **app directory**,
    so the project's `opencode.json` applies — including the matrix MCP
    tools (`send_message`, `wait_for_reply`, `read_messages`, …)
  - `timeout` — seconds to wait for the answer (default 300)
  - `keep_session` — keep the session after a *successful* run too
    (default false)
- `context` — how many messages `{{msg}}` holds (default `25`, max `100`).
- **The answer is posted by the webhooker itself** — the final assistant
  text becomes a room message. The agent can *additionally* use the matrix
  MCP tools mid-run (e.g. to ask a clarifying question and
  `wait_for_reply` for the answer).
- A trigger fires only for a message **from someone else** — messages the
  bot itself sent are always skipped (no loops).
- If a client is **waiting** for that room at the moment (see below), the
  message goes to the waiting client *instead of* the triggers.
- Runs in a background thread; long model turns (the watcher only polls
  and waits) don't block the watcher. Restart the server after editing
  `bots.json` to load changes.

### Waiting for a reply

Ask a question and block for the answer instead of firing a trigger:

- **MCP tool** `wait_for_reply(room, bot="", timeout=120)` — blocks up to
  `timeout` seconds (max 600) for the next message in the room that is not
  from the bot itself: `{"received": {"sender", "body", "ts", ...}}`, or
  `{"received": null}` on timeout. Natural pattern for a conversational
  trigger: the action's agent can `send_message` a clarifying
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

1. interrupts the running action (OpenCode `session/<id>/interrupt` — the
   model turn ends and no answer is posted);
2. releases any model blocked in `wait_for_reply` for that room
   (`POST /wait` returns `{"stopped": true, "received": null}`);
3. removes every wrench reaction it added for that room;
4. replies with a short confirmation (`⏹ Stopped.` / `⏹ Stop sent …`).

> Since actions run inside the OpenCode server (nothing is spawned per
> message), stopping is an API interrupt — the old `opencode run
> --standalone` process-group caveat is gone.

### Reasoning effort (`/effort`)

`/effort` in a room lists the effort levels (and what the room currently
uses); `/effort <level>` sets one **for that room** — the leading `/` is
required, the whole message must be the command:

| level | OpenCode model variant |
|-----------|------------------------|
| `default` | the model's own default (resets the override) |
| `low` | `low` |
| `mid` | `medium` |
| `high` | `high` |
| `xhigh` | `xhigh` |

The setting is remembered per room (in the bot's state dir, so it survives
restarts) and becomes the model `variant` of every action run started in
that room afterwards — trigger runs and voice-call actions alike. If the
action pins no `model`, the server's default model for the session's
directory is resolved (`GET /api/model/default`) so the variant can still
be applied.

### Interjections (`/btw`)

`/btw <text>` is a Zwischenruf for the action(s) **currently running** in
the room: instead of starting a new run (or feeding a `wait_for_reply`
client), the text is admitted to the running OpenCode session as a `steer`
prompt, so the model sees it mid-turn. If nothing is running the bot says
so; the message gets the usual 🔧 while it is being handed over. (Unlike a
plain "btw …" in chat, which stays a normal message, the `/` is required.)

## Voice calls (MatrixRTC / Element Call)

A bot can answer the calls your Matrix client places in a room: it joins the
call, listens, and answers out loud.

```
you ──WebRTC──▶ LiveKit SFU ──▶ bot: whisper (STT) ──▶ your command ──▶ TTS ──▶ you
```

Per bot in `bots.json` (needs `requirements-call.txt` installed and the
`VOICE_*` settings in `.env` — see `.env.example`):

```json
"call": {
  "enabled": true,
  "action": {
    "template": "voice",
    "prompt": "Gehörte Frage: {{transcript}}",
    "timeout": 90
  },
  "greeting": "Hallo, ich bin der Bot.",
  "rooms": ["Some Room"],        // optional; default: every room the bot is in
  "max_seconds": 600,
  "language": "de"
}
```

What has to be true for a call to work:

- **Your client places MatrixRTC calls** (Element Call — the default in
  current Element). The homeserver advertises the LiveKit transport in
  `.well-known` (`org.matrix.msc4143.rtc_foci`) and Synapse needs
  `msc4143_enabled` + a JWT service.
- **Power level ≥ 50 in that room — for you *and* the bot.** Starting or
  joining a call means sending a call-membership *state* event, and
  `state_default` is 50. Greyed-out call buttons in a room where you sit at
  `users_default: 0` come from exactly this (rooms an old bot account created:
  `_fix_power_levels.py` repairs them).
- The bot mirrors the caller's membership event — state key
  (`_@user_device_m.call` in current Element Call), `call_id`, `foci_preferred`,
  intent and expiry — and swaps in its own identity
  (`org.matrix.msc3401.call.member` / `org.matrix.msc4143.rtc.member`). It then
  exchanges an OpenID token for a LiveKit JWT and joins the SFU. No TURN server
  is needed — media goes through the SFU.
- **The LiveKit identity has to be the one the dialect implies.** For the
  MSC3401 shape that is `userId:deviceId`, obtained from the JWT service's
  older `/sfu/get`; the bot publishes the same string as `membershipID`. If the
  two ever disagree, the call still "works" — the bot hears you, answers in
  text — but your client has no membership to attach the bot's audio track to,
  so **you hear nothing**. The log line says both:
  `joined … as @bot:host:DEVICE (state key '_@bot:host_DEVICE_m.call')`.

How it behaves:

- Each utterance (energy VAD: `vad_threshold`, `silence_end`, `min_speech`,
  `max_utterance`) is sent to whisper. The action's prompt gets the usual
  placeholders plus **`{{transcript}}`**, and its **final assistant text is
  what gets spoken** — keep answers short, TTS costs roughly 3× the audio
  length.
- Turn taking: while the bot speaks, incoming audio has to be
  `echo_factor` (default 2.5) times louder than `vad_threshold` to count —
  that is its own voice coming back through your microphone. Speak clearly
  over it for `barge_in` seconds (default 0.4) and the bot stops talking and
  listens. 0.8 s of audio before every utterance is kept, so an interruption
  that begins mid-greeting does not lose its first words. The greeting is
  skipped when you started speaking first.
- Voice: `VOICE_TTS_INSTRUCTION` (default `Professionell, deutsch.`) goes to
  qwen3-tts as `instructions`, `VOICE_LANGUAGE` as an ISO `language` code.
  The greeting and the thinking cue are synthesized once and cached
  (`VOICE_CACHE_DIR`, default `<state dir>/voice-cache`): synthesizing them at
  join time costs ~9 s, long enough for the caller to have asked their
  question already — which is exactly what it used to sound like.
- Where the time goes: tts on this gateway takes **~3.5× the length of what it
  speaks**, does not stream (the first byte arrives once the clip is finished)
  and serializes — two concurrent requests take exactly as long as two in a
  row.  So the length of the answer *is* the latency: `max_answer_chars`
  (default 220) clips it and the command prompt should ask for one short
  sentence.  Every stage logs its own timing:

  ```
  [speech] stt: 2.1s of audio -> 52 chars in 2.4s
  [call:…] heard: Wie viel Grad Celsius ist es gerade in Gelsenkirchen?
  [call:…] agent answered in 41.2s
  [speech] tts: 118 chars -> 7.9s of audio in 27.4s (first byte 27.3s, 380 KB)
  [call:…] speaking 7.9s: Aktuell sind es 12 Grad. (+71.0s after the caller)
  ```

  The agent is usually the slow part (it may search the web), so
  `action.timeout` (default 90) interrupts an action that runs longer and
  speaks `sorry_line` instead of leaving the caller in silence.
  The prompt is plain text — placeholders are inserted literally, there is
  no shell quoting to think about.
- A bare `stop` in the room hangs the call up; so does the last other
  participant leaving, the MatrixRTC slot closing, or `max_seconds`.
- `GET /health` lists the active calls per bot.
- No client needed to test it: `python3 _call_selftest.py` runs a fake caller
  against the real code in a throw-away room the bot creates itself.
- Media encryption: the bot joins the SFU without MatrixRTC media keys, so
  calls work in rooms **without** end-to-end encryption (which is also what
  the rest of this bridge assumes).

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
