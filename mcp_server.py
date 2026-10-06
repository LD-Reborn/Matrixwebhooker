#!/usr/bin/env python3
"""Matrix MCP server — exposes the Matrix bots of this project to MCP clients.

Runs over stdio (the default MCP transport), so it is started per client:
register it in OpenCode / Claude Desktop / etc. and call it as a plain
`python3 mcp_server.py` command. It talks to the homeserver directly —
the HTTP server (server.py) does NOT need to be running.

Tools:
    list_bots        configured bot accounts + their alert rooms
    list_rooms       the bot's joined rooms (id + name)
    read_messages    recent messages of a room
    send_message     send a message (m.text / m.notice, optional HTML)
    react            add an emoji reaction to a specific message
    wait_for_reply   block for the next message in a room (that message is
                     delivered to you and skips the bot's configured triggers)

Environment:
    MCP_BOT               default bot to use when a tool doesn't get one
    MATRIX_WEBHOOKER_URL  base URL of the HTTP server for wait_for_reply
                          (default http://localhost:8080)
    (config: same .env + bots.json as server.py, BOTS_JSON to relocate)

Example client registration (OpenCode / generic MCP config):

    {
      "mcp": {
        "matrix": {
          "type": "local",
          "command": ["python3", "/path/to/MatrixWebhooker/mcp_server.py"],
          "enabled": true
        }
      }
    }
"""

import os
import sys

import requests
from dotenv import load_dotenv

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "ERROR: the 'mcp' package is required. Install with: pip install 'mcp[cli]'\n")
    raise

from matrixbot import BASE_DIR, Bot, ConfigError, MatrixError, load_settings

load_dotenv(BASE_DIR / ".env")

try:
    settings = load_settings()
except ConfigError as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    sys.exit(2)

BOTS: dict[str, Bot] = {cfg.name: Bot(cfg, settings) for cfg in settings.bots}
DEFAULT_BOT = os.environ.get("MCP_BOT", "").strip()
if DEFAULT_BOT and DEFAULT_BOT not in BOTS:
    print(f"ERROR: MCP_BOT='{DEFAULT_BOT}' is not a known bot "
          f"(known: {', '.join(BOTS)})", file=sys.stderr)
    sys.exit(2)

mcp = FastMCP("matrix-webhooker")

#: wait_for_reply is served by the long-lived HTTP server (server.py), which
#: owns the message watcher and the trigger/wait registry.
WEBHOOKER_URL = os.environ.get(
    "MATRIX_WEBHOOKER_URL", "http://localhost:8080").rstrip("/")
MAX_WAIT_TIMEOUT = 600


def _resolve_bot(bot: str = "") -> Bot:
    name = (bot or DEFAULT_BOT or next(iter(BOTS), "")).strip()
    # Accept a full matrix user id (@grafana_bot:domain) too.
    if name.startswith("@"):
        name = name[1:].split(":", 1)[0]
    selected = BOTS.get(name)
    if selected is None:
        known = ", ".join(f"'{k}'" for k in BOTS)
        raise ValueError(f"unknown bot {name!r} (known bots: {known})")
    return selected


def _room_or_room_id(bot: Bot) -> str | None:
    return bot.room_id_cached_or_pinned()


@mcp.tool()
def list_bots() -> list[dict]:
    """List the configured Matrix bot accounts and their alert room (if any)."""
    return [
        {"name": b.name, "user": b.user_id, "room": _room_or_room_id(b)}
        for b in BOTS.values()
    ]


@mcp.tool()
def list_rooms(bot: str = "") -> dict:
    """List the rooms the bot is in (room_id + display name)."""
    b = _resolve_bot(bot)
    try:
        rooms = b.client.list_rooms()
    except MatrixError as exc:
        raise ValueError(f"matrix list rooms failed: {exc}") from exc
    return {"bot": b.name, "rooms": rooms}


@mcp.tool()
def read_messages(room: str, bot: str = "", limit: int = 50) -> dict:
    """Read the most recent messages (up to `limit`, max 100, oldest first)
    of a room. Use list_rooms / list_bots to find the room_id first."""
    b = _resolve_bot(bot)
    limit = max(1, min(int(limit), 100))
    try:
        messages = b.client.read_messages(room, limit=limit, crypto=b.crypto)
    except MatrixError as exc:
        raise ValueError(f"matrix read failed: {exc}") from exc
    return {"bot": b.name, "room": room, "messages": messages}


@mcp.tool()
def send_message(text: str, bot: str = "", room: str = "",
                 msgtype: str = "m.text", html: str = "") -> dict:
    """Send a Matrix message. `room` may be a room_id (or left empty to use
    the bot's alert room). msgtype: 'm.text' (default) or 'm.notice'.
    `html` optionally provides an HTML body."""
    b = _resolve_bot(bot)
    if not text or not text.strip():
        raise ValueError("'text' must be a non-empty string")
    if msgtype not in ("m.text", "m.notice"):
        raise ValueError("msgtype must be 'm.text' or 'm.notice'")
    payload: dict = {"msgtype": msgtype, "body": text}
    if html:
        payload["format"] = "org.matrix.custom.html"
        payload["formatted_body"] = html
    try:
        room_id = room or b.ensure_room()
        event = b.client.send_message(room_id, payload)
    except MatrixError as exc:
        raise ValueError(f"matrix send failed: {exc}") from exc
    return {"ok": True, "bot": b.name, "room": room_id,
            "event_id": event.get("event_id")}


@mcp.tool()
def react(event_id: str, key: str, room: str = "", bot: str = "") -> dict:
    """React with an emoji to a specific message.

    `event_id`: the "$..." id of the message to react to (see the id returned
    by read_messages). `key`: the emoji to add, e.g. "🔧", "✅" or "👍".
    `room`: the room id (!...) the message lives in.
    Returns the id of the reaction event (you can keep it to reference later).
    Note: Matrix has no simple "remove by emoji + message" call, so this tool
    only *adds* a reaction; the "wrench while handled" removal is done
    automatically by the watcher itself."""
    b = _resolve_bot(bot)
    room = room.strip()
    if not room.startswith("!"):
        raise ValueError("'room' must be a Matrix room id (!...) — use list_rooms first")
    event_id = event_id.strip()
    if not event_id.startswith("$"):
        raise ValueError("'event_id' must be a Matrix event id ($...) — see read_messages")
    key = key.strip()
    if not key:
        raise ValueError("'key' (the emoji) must be a non-empty string")
    try:
        reaction_event_id = b.client.react(room, event_id, key)
    except MatrixError as exc:
        raise ValueError(f"matrix react failed: {exc}") from exc
    return {"ok": True, "bot": b.name, "room": room, "event_id": event_id,
            "key": key, "reaction_event_id": reaction_event_id}


@mcp.tool()
def wait_for_reply(room: str, bot: str = "", timeout: int = 120) -> dict:
    """Block up to `timeout` seconds (default 120, max 600) for the next
    message in `room` (a Matrix room id from list_rooms; the bot must be in
    it) that is NOT sent by the bot itself, and return it as {"received":
    {...message...}}. On timeout: {"received": null}.

    Use it to ask a question with send_message and then wait for the answer.
    While you wait, the received message is handed to you and is deliberately
    NOT fired through the bot's configured triggers in bots.json.

    Requires the matrix-webhooker HTTP server to be running (python3
    server.py, or docker compose); MATRIX_WEBHOOKER_URL redirects."""
    b = _resolve_bot(bot)
    room = room.strip()
    if not room.startswith("!"):
        raise ValueError("'room' must be a Matrix room id (!...) — use list_rooms first")
    timeout = max(1, min(int(timeout), MAX_WAIT_TIMEOUT))
    try:
        resp = requests.post(
            f"{WEBHOOKER_URL}/wait",
            headers={"X-API-Key": b.cfg.api_key},
            json={"room_id": room, "timeout": timeout},
            timeout=timeout + 20,
        )
    except requests.exceptions.RequestException as exc:
        raise ValueError(
            f"matrix-webhooker server unreachable at {WEBHOOKER_URL} ({exc}). "
            "wait_for_reply is served by the long-lived server.py process (it "
            "owns the triggers); start it (python3 server.py / docker compose) "
            "or set MATRIX_WEBHOOKER_URL.") from exc
    if resp.status_code >= 400:
        raise ValueError(f"matrix-webhooker error ({resp.status_code}): "
                         f"{resp.text.strip()[:300]}")
    return resp.json()


if __name__ == "__main__":
    mcp.run()
