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

Environment:
    MCP_BOT     default bot to use when a tool doesn't get one
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
        messages = b.client.read_messages(room, limit=limit)
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


if __name__ == "__main__":
    mcp.run()
