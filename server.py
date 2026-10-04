#!/usr/bin/env python3
"""Matrix Webhooker — tiny multi-bot HTTP API for alerting into Matrix.

Each request is authenticated with the API key of the bot account it should
act as (X-API-Key header or ``Authorization: Bearer``). See ``bots.json``.

Endpoints:
    POST /webhook    send a message as the requesting bot
         body: {"text": "...",
                "msgtype": "m.text (default) | m.notice",
                "html": "optional HTML body",
                "room_id": "optional override of the bot's default room"}
    POST /wait       block for the bot's next message in a room; that message
                     skips the bot's triggers (body: {"room_id": "!...", "timeout": 120})
    GET  /me         which bot this API key belongs to (+ its triggers)
    GET  /rooms      the requesting bot's joined rooms (id + name)
    GET  /rooms/<room_id>/messages
                     recent messages of the bot's room (?limit=50, max 100)
    GET  /health     service + per-bot status (no auth)
    GET  /           usage hints (no auth)


Message watching / triggers:
    One watcher thread per bot (triggers.py) long-polls /sync and, for every
    room message the bot receives (not from itself):
      * a bare "stop" keyword (stop / /stop / STOP / STOP!!! -- starts with
        "stop", no whitespace) stops the current execution for that room:
        kills the running trigger command, releases waiting models, removes
        the wrench reactions, and replies with a short confirmation;
      * otherwise it either hands the message to a client waiting via /wait,
        or fires the bot's configured triggers -- bash command line(s) per
        room with {{channel}}/{{msg}}/{{sender}}/{{room}}.
    While a message is being handled the bot reacts to it with 🔧 and
    removes that reaction again as soon as handling is finished.
Config: ``.env`` (shared settings) + ``bots.json`` (bot roster, secrets).
"""

import hmac
import os
import sys

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from matrixbot import BASE_DIR, Bot, ConfigError, MatrixError, load_settings
from triggers import MAX_WAIT_SECONDS, MessageWatcher, WaitRegistry

load_dotenv(BASE_DIR / ".env")

try:
    settings = load_settings()
except ConfigError as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    sys.exit(2)

BOTS: dict[str, Bot] = {cfg.name: Bot(cfg, settings) for cfg in settings.bots}

#: Pending reply waiters (POST /wait); incoming messages matching a waiter
#: are delivered there and are NOT fired as triggers.
WAITERS = WaitRegistry()

app = Flask(__name__)


def _key_matches(candidate: str, stored: str) -> bool:
    if not candidate.isascii():
        return False
    try:
        return hmac.compare_digest(candidate, stored)
    except (TypeError, ValueError):
        return False


def _authed_bot() -> Bot | None:
    """Identify the bot by its API key (constant-time compare per key)."""
    key = request.headers.get("X-API-Key", "")
    if not key:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
    if not key:
        return None
    for bot in BOTS.values():
        if _key_matches(key, bot.cfg.api_key):
            return bot
    return None


@app.get("/")
def index():
    return jsonify({
        "service": "matrix-webhooker",
        "bots": [b.name for b in BOTS.values()],
        "endpoints": {
            "POST /webhook": {
                "auth": "header X-API-Key: <that bot's api_key>",
                "body": {"text": "alert message",
                         "msgtype": "m.text (default) | m.notice",
                         "html": "optional HTML body",
                         "room_id": "optional override of the bot's default room"},
            },
            "POST /wait": {
                "auth": "same API key (names the bot that waits)",
                "body": {"room_id": "!...:domain",
                         "timeout": "seconds, default 120, max 600"},
                "note": "blocks until the next message in that room (not from "
                        "the bot itself); that message is delivered here and "
                        "skips the bot's triggers; timeout -> received=null; "
                        "stopped=true if a 'stop' command released it first",
            },
            "GET /me": "which bot an API key belongs to (with its triggers)",
            "GET /rooms": "the requesting bot's joined rooms",
            "GET /rooms/<room_id>/messages": "recent messages (?limit=50, max 100)",
            "GET /health": "service + per-bot status, no auth",
        },
        "matrix": settings.homeserver,
    })


@app.get("/health")
def health():
    bots_status = [b.status() for b in BOTS.values()]
    ok = bool(BOTS)
    return jsonify({"ok": ok, "bots": bots_status, "matrix": settings.homeserver}), \
        (200 if ok else 503)


@app.get("/me")
def me():
    bot = _authed_bot()
    if bot is None:
        return jsonify({"error": "unauthorized: send a valid bot API key as "
                                 "X-API-Key (or Authorization: Bearer ...)"}), 401
    return jsonify({"bot": bot.name, "user": bot.user_id,
                    "room": bot.room_id_cached_or_pinned(),
                    "triggers": [{"room": t.room, "context": t.context,
                                  "commands": t.commands}
                                 for t in bot.cfg.triggers]})


@app.get("/rooms")
def rooms():
    bot = _authed_bot()
    if bot is None:
        return jsonify({"error": "unauthorized: send a valid bot API key as "
                                 "X-API-Key (or Authorization: Bearer ...)"}), 401
    try:
        joined = bot.client.list_rooms()
    except MatrixError as exc:
        return jsonify({"error": f"matrix list rooms failed: {exc}"}), 502
    return jsonify({"bot": bot.name, "rooms": joined})


@app.get("/rooms/<room_id>/messages")
def messages(room_id: str):
    bot = _authed_bot()
    if bot is None:
        return jsonify({"error": "unauthorized: send a valid bot API key as "
                                 "X-API-Key (or Authorization: Bearer ...)"}), 401
    try:
        limit = request.args.get("limit", 50, type=int)
        events = bot.client.read_messages(room_id, limit=limit)
    except MatrixError as exc:
        return jsonify({"error": f"matrix read failed: {exc}"}), 502
    return jsonify({"bot": bot.name, "room": room_id, "messages": events})


@app.post("/webhook")
def webhook():
    bot = _authed_bot()
    if bot is None:
        return (jsonify({"error": "unauthorized: send a valid bot API key as "
                                  "X-API-Key (or Authorization: Bearer ...)"}),
                401)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "body must be a JSON object"}), 400

    text = data.get("text") or data.get("msg")
    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "missing required field 'text'"}), 400

    msgtype = data.get("msgtype") or "m.text"
    if msgtype not in ("m.text", "m.notice"):
        return jsonify({"error": "msgtype must be m.text or m.notice"}), 400

    payload: dict = {"msgtype": msgtype, "body": text}
    if data.get("html"):
        if not isinstance(data["html"], str):
            return jsonify({"error": "'html' must be a string"}), 400
        payload["format"] = "org.matrix.custom.html"
        payload["formatted_body"] = data["html"]

    try:
        room_id = data.get("room_id") or bot.ensure_room()
        event = bot.client.send_message(room_id, payload)
    except MatrixError as exc:
        return jsonify({"error": f"matrix operation failed: {exc}"}), 502
    return jsonify({"ok": True, "bot": bot.name, "room": room_id,
                    "event_id": event.get("event_id")})


@app.post("/wait")
def wait_reply():
    """Block until the bot's next message in a room (or timeout).

    The message is delivered here and is therefore NOT fired through the
    bot's triggers (see ``triggers.py`` / the MCP ``wait_for_reply`` tool).
    """
    bot = _authed_bot()
    if bot is None:
        return (jsonify({"error": "unauthorized: send a valid bot API key as "
                                  "X-API-Key (or Authorization: Bearer ...)"}),
                401)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "body must be a JSON object"}), 400
    room_id = str(data.get("room_id") or "").strip()
    if not room_id.startswith("!"):
        return jsonify({"error": "'room_id' must be a Matrix room id (!...)"}), 400
    try:
        timeout = float(data.get("timeout", 120))
    except (TypeError, ValueError):
        return jsonify({"error": "'timeout' must be a number of seconds"}), 400
    timeout = max(1.0, min(timeout, MAX_WAIT_SECONDS))

    waiter = WAITERS.register(bot.name, room_id)
    if waiter.event.wait(timeout):
        return jsonify({"ok": True, "bot": bot.name, "room": room_id,
                        "received": waiter.message, "stopped": waiter.stopped})
    return jsonify({"ok": True, "bot": bot.name, "room": room_id,
                    "received": None, "stopped": False})


def main() -> None:
    watchers: list[MessageWatcher] = []
    for bot in BOTS.values():
        trig = [t.room for t in bot.cfg.triggers]
        print(f"bot: {bot.name} ({bot.user_id}) "
              f"room={bot.room_id_cached_or_pinned() or '(resolved on first use)'} "
              f"triggers=[{', '.join(trig) or 'none'}]")
        w = MessageWatcher(bot, WAITERS)
        w.start()
        watchers.append(w)
    print(f"Webhooker ready: {len(BOTS)} bot(s) on {settings.homeserver} "
          f"({len(watchers)} watcher(s) active)")
    app.run(host=settings.host, port=settings.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
