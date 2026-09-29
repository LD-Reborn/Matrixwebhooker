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
    GET  /me         which bot this API key belongs to
    GET  /health     service + per-bot status (no auth)
    GET  /           usage hints (no auth)

Config: ``.env`` (shared settings) + ``bots.json`` (bot roster, secrets).
"""

import hmac
import os
import sys

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from matrixbot import BASE_DIR, Bot, ConfigError, MatrixError, load_settings

load_dotenv(BASE_DIR / ".env")

try:
    settings = load_settings()
except ConfigError as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    sys.exit(2)

BOTS: dict[str, Bot] = {cfg.name: Bot(cfg, settings) for cfg in settings.bots}

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
            "GET /me": "which bot an API key belongs to",
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
                    "room": bot.room_id_cached_or_pinned()})


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


def main() -> None:
    for bot in BOTS.values():
        print(f"bot: {bot.name} ({bot.user_id}) "
              f"room={bot.room_id_cached_or_pinned() or '(resolved on first use)'}")
    print(f"Webhooker ready: {len(BOTS)} bot(s) on {settings.homeserver}")
    app.run(host=settings.host, port=settings.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
