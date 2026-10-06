#!/usr/bin/env python3
"""Shared bot model: config loading + per-bot Matrix client.

Used by both ``server.py`` (HTTP API) and ``mcp_server.py`` (MCP tools).

Configuration
-------------
``.env``       shared settings: homeserver, invite user, HTTP bind (no secrets)
``bots.json``  roster of bot accounts; each bot has its own Matrix login
               and its own API key for the webhook API (see bots.example.json)

Per-bot runtime state (cached access token, remembered room id) is stored
under ``MATRIX_STATE_DIR/<bot-name>/`` (defaults to the app directory).
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

from crypto import BotCrypto, CryptoError

BASE_DIR = Path(__file__).resolve().parent


class ConfigError(RuntimeError):
    """Raised when the configuration is missing or invalid."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class Trigger:
    """Fires when the bot receives a message (not from itself) in one room.

    ``room`` matches the room's display name (case-insensitive) or a room id
    (``!...``) — room ids are the reliable way to match DMs. ``commands`` are
    bash command line(s) run in sequence in the app directory; the
    placeholders ``{{channel}}`` (room name), ``{{msg}}`` (last ``context``
    messages, newest last), ``{{sender}}`` and ``{{room}}`` (room id) are
    substituted shell-quoted.
    """
    room: str
    commands: list[str]
    context: int = 25               # messages included in {{msg}} (max 100)

    def matches(self, room_id: str, room_name: str | None) -> bool:
        if self.room.startswith("!"):
            return self.room.casefold() == room_id.casefold()
        return room_name is not None and self.room.casefold() == room_name.casefold()


@dataclass
class BotConfig:
    name: str                       # label; also used for the state subdir
    user: str                       # Matrix localpart (no @, no domain)
    password: str
    api_key: str                    # required on /webhook (X-API-Key or Bearer)
    room_id: str | None = None      # pinned room; null = remember/cache/auto-create
    room_name: str | None = None    # name for the auto-created room
    triggers: list[Trigger] = field(default_factory=list)


@dataclass
class Settings:
    homeserver: str
    invite_user: str | None         # invited into auto-created rooms
    host: str
    port: int
    state_dir: Path
    bots: list[BotConfig] = field(default_factory=list)


def bots_file() -> Path:
    return Path(os.environ.get("BOTS_JSON", str(BASE_DIR / "bots.json")))


def load_settings() -> Settings:
    homeserver = os.environ.get("MATRIX_HOME_SERVER", "").strip().rstrip("/")
    if not homeserver:
        raise ConfigError("MATRIX_HOME_SERVER is not set (see .env.example)")

    try:
        port = int(os.environ.get("PORT", "8080"))
    except ValueError:
        raise ConfigError("PORT must be an integer (see .env.example)")

    state_dir = Path(os.environ.get("MATRIX_STATE_DIR", str(BASE_DIR)))
    invite_user = os.environ.get("MATRIX_INVITE_USER", "").strip() or None

    path = bots_file()
    if not path.exists():
        raise ConfigError(
            f"bot roster not found at {path} (copy bots.example.json and fill it in)")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}")

    entries = raw.get("bots")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{path} must contain a non-empty \"bots\" list")

    bots: list[BotConfig] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: bots[{i}] must be an object")
        name = str(entry.get("name", "")).strip() or f"bot{i + 1}"
        user = str(entry.get("user", name)).strip()
        password = str(entry.get("password", "")).strip()
        api_key = str(entry.get("api_key", "")).strip()
        if not user:
            raise ConfigError(f"{path}: bots[{i}] ('{name}'): 'user' is required")
        if not password:
            raise ConfigError(f"{path}: bots[{i}] ('{name}'): 'password' is required")
        if not api_key:
            raise ConfigError(
                f"{path}: bots[{i}] ('{name}'): 'api_key' is required. "
                "Generate one: python3 -c 'import secrets; print(secrets.token_urlsafe(24))'")
        room_id = str(entry.get("room_id") or "").strip() or None
        if room_id and not room_id.startswith("!"):
            raise ConfigError(f"{path}: bots[{i}] ('{name}'): room_id must start with '!...'")
        rooms = [b.name for b in bots]
        if name in rooms:
            raise ConfigError(f"{path}: duplicate bot name '{name}'")
        if any(b.api_key == api_key for b in bots):
            raise ConfigError(f"{path}: duplicate api_key for bot '{name}'")
        raw_triggers = entry.get("triggers", [])
        if not isinstance(raw_triggers, list):
            raise ConfigError(f"{path}: bots[{i}] ('{name}'): 'triggers' must be a list")
        triggers: list[Trigger] = []
        for j, t in enumerate(raw_triggers):
            if not isinstance(t, dict):
                raise ConfigError(f"{path}: bots[{i}] ('{name}').triggers[{j}] must be an object")
            troom = str(t.get("room", "")).strip()
            if not troom:
                raise ConfigError(
                    f"{path}: bots[{i}] ('{name}').triggers[{j}]: 'room' (name or '!...' id) is required")
            raw_cmd = t.get("command", t.get("commands"))
            if isinstance(raw_cmd, str):
                cmds = [raw_cmd]
            elif isinstance(raw_cmd, list):
                cmds = [str(c) for c in raw_cmd]
            else:
                raise ConfigError(
                    f"{path}: bots[{i}] ('{name}').triggers[{j}]: "
                    "'command' must be a string or a list of strings")
            if not any(c.strip() for c in cmds):
                raise ConfigError(f"{path}: bots[{i}] ('{name}').triggers[{j}]: "
                                  "at least one non-empty command is required")
            try:
                context = int(t.get("context", 25))
            except (TypeError, ValueError):
                raise ConfigError(f"{path}: bots[{i}] ('{name}').triggers[{j}]: "
                                  "'context' must be an integer")
            if not 1 <= context <= 100:
                raise ConfigError(f"{path}: bots[{i}] ('{name}').triggers[{j}]: "
                                  "'context' must be between 1 and 100")
            triggers.append(Trigger(room=troom, commands=cmds, context=context))
        bots.append(BotConfig(
            name=name,
            user=user,
            password=password,
            api_key=api_key,
            room_id=room_id,
            room_name=str(entry.get("room_name") or "").strip() or None,
            triggers=triggers,
        ))

    return Settings(
        homeserver=homeserver,
        invite_user=invite_user,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=port,
        state_dir=state_dir,
        bots=bots,
    )


def default_room_name(bot_name: str) -> str:
    """'grafana_bot' -> 'Grafana Bot Alerts'"""
    pretty = " ".join(p.capitalize() for p in bot_name.replace("-", "_").split("_") if p)
    return f"{pretty} Alerts"


# ---------------------------------------------------------------------------
# Minimal Matrix client-REST wrapper (one instance per bot)
# ---------------------------------------------------------------------------


class MatrixError(RuntimeError):
    def __init__(self, status: int, payload):
        self.status = status
        self.payload = payload
        detail = payload.get("error", payload) if isinstance(payload, dict) else payload
        super().__init__(f"HTTP {status}: {detail}")


def _safe_json(resp: requests.Response):
    try:
        return resp.json()
    except ValueError:
        return resp.text


class Matrix:
    def __init__(self, homeserver: str, user: str, password: str,
                 state_dir: Path, timeout: float = 15.0):
        self.hs = homeserver
        self.user = user
        self.password = password
        self.timeout = timeout
        self.state_dir = state_dir
        self.token: str | None = None
        self.device_id: str | None = None
        self.http = requests.Session()
        self._login_lock = threading.Lock()   # guards concurrent lazy logins
        self._load_stored_token()

    @property
    def user_id(self) -> str:
        return f"@{self.user}:{urlsplit(self.hs).netloc}"

    # -- token cache (state_dir/.token) --------------------------------------

    def _load_stored_token(self) -> None:
        try:
            with open(self.state_dir / ".token", encoding="utf-8") as fh:
                lines = [line.strip() for line in fh if line.strip()]
            if lines:
                self.token = lines[0]
                self.device_id = lines[1] if len(lines) > 1 else None
        except OSError:
            pass

    def _save_token(self) -> None:
        try:
            fd = os.open(self.state_dir / ".token", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self.token + "\n")
                if self.device_id:
                    fh.write(self.device_id + "\n")
        except OSError as exc:
            print(f"WARNING: could not save token cache: {exc}", file=sys.stderr)

    def _clear_token(self) -> None:
        self.token = None
        try:
            os.remove(self.state_dir / ".token")
        except OSError:
            pass

    # -- login (deliberately lazy: first authenticated call logs in) ----------

    def _ensure_token(self) -> None:
        """Double-checked: safe to call concurrently from any thread."""
        if self.token:
            return
        with self._login_lock:
            if not self.token:
                self.login()

    def _relogin(self) -> None:
        with self._login_lock:
            self._clear_token()
            self.login()

    def login(self, retries: int = 3) -> None:
        flows = self._plain_request("GET", f"{self.hs}/_matrix/client/v3/login")["flows"]
        if not any(f.get("type") == "m.login.password" for f in flows):
            raise MatrixError(0, "homeserver does not offer a m.login.password flow")
        body = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": self.user},
            "password": self.password,
        }
        for attempt in range(retries + 1):
            resp = self.http.post(f"{self.hs}/_matrix/client/v3/login", json=body,
                                  timeout=self.timeout)
            if resp.status_code == 429 and attempt < retries:
                # Rate-limited (M_LIMIT_EXCEEDED): honor retry_after_ms and try again.
                try:
                    wait_ms = int(resp.json().get("retry_after_ms", 5000))
                except ValueError:
                    wait_ms = 5000
                wait_s = wait_ms // 1000 + 1
                print(f"Login rate-limited, waiting {wait_s}s before retry ...",
                      file=sys.stderr)
                time.sleep(wait_s)
                continue
            if resp.status_code >= 400:
                raise MatrixError(resp.status_code, _safe_json(resp))
            data = resp.json()
            self.token = data["access_token"]
            self.device_id = data.get("device_id")
            self._save_token()
            print(f"[{self.user}] logged in (device: {self.device_id or 'unknown'})",
                  file=sys.stderr)
            return
        raise MatrixError(0, "could not log in (still rate-limited after retries)")

    # -- simple account/room operations ---------------------------------------

    def whoami(self) -> dict:
        return self._request("GET", f"{self.hs}/_matrix/client/v3/account/whoami")

    def list_rooms(self) -> list[dict]:
        ids = self._request("GET",
                            f"{self.hs}/_matrix/client/v3/joined_rooms")["joined_rooms"]
        rooms = []
        for rid in ids:
            try:
                state = self._request("GET", f"{self.hs}/_matrix/client/v3/rooms/"
                                             f"{quote(rid, safe='')}/state/m.room.name/")
                name = state.get("name")
            except MatrixError:
                name = None
            rooms.append({"room_id": rid, "name": name})
        return rooms

    def read_messages(self, room_id: str, limit: int = 50,
                      crypto: BotCrypto | None = None) -> list[dict]:
        """Last ``limit`` room events of type m.room.message, oldest first.

        Encrypted events are decrypted when ``crypto`` is given; events that
        cannot be decrypted are skipped.
        """
        limit = max(1, min(int(limit), 100))
        data = self._request("GET", f"{self.hs}/_matrix/client/v3/rooms/"
                                    f"{quote(room_id, safe='')}"
                                    f"/messages?dir=b&limit={limit}")
        out = []
        for ev in reversed(data.get("chunk", [])):
            etype = ev.get("type")
            if etype == "m.room.message":
                content = ev.get("content", {})
            elif etype == "m.room.encrypted" and crypto is not None:
                try:
                    content = crypto.decrypt_room_content(ev.get("content", {}))
                except CryptoError:
                    continue
            else:
                continue
            body = content.get("body")
            if not isinstance(body, str):
                continue
            out.append({
                "event_id": ev.get("event_id"),
                "sender": ev.get("sender"),
                "msgtype": content.get("msgtype", "m.text"),
                "body": body,
                "ts": ev.get("origin_server_ts"),
            })
        return out

    def sync(self, since: str | None = None, timeout_ms: int = 30000) -> dict:
        """One (long-polling) /sync round trip.

        Returns ``{"next_batch": ..., "events": [...], "to_device": [...]}``.
        Events are the *new* m.room.message / m.room.encrypted events of the
        joined rooms (oldest first, per room), each with ``room_id``,
        ``room_name``, ``event_id``, ``sender``, ``ts`` and, for plaintext
        ones, ``msgtype``/``body``; encrypted ones carry ``raw`` (Megolm
        material) for decryption by ``Bot.crypto``. ``to_device`` holds the
        raw to-device events (Olm/Megolm key material). A call without
        ``since`` is a snapshot: its events are historical and the caller
        must not act on them.
        """
        flt = json.dumps(
            {"room": {
                "timeline": {"types": ["m.room.message", "m.room.encrypted"], "limit": 100},
                "state": {"types": ["m.room.name", "m.room.canonical_alias"]},
            },
             "to_device": {"types": ["*"]}},
            separators=(",", ":"))
        params: dict[str, str] = {
            "timeout": str(max(0, min(int(timeout_ms), 30000))),
            "filter": flt,
        }
        if since:
            params["since"] = since
        # The long-poll can hold up to timeout_ms; give the HTTP client more slack.
        data = self._request(
            "GET", f"{self.hs}/_matrix/client/v3/sync",
            params=params, timeout=max(1.0, timeout_ms / 1000.0 + 10.0))
        events: list[dict] = []
        for room_id, rdata in (data.get("rooms", {}).get("join", {}) or {}).items():
            name = (rdata.get("name") or {}).get("name")
            if not name:
                # Some Synapse versions omit the convenience field: read it
                # from the state events (included by the filter above).
                for se in ((rdata.get("state", {}) or {}).get("events", [])) or []:
                    if se.get("type") == "m.room.name":
                        name = (se.get("content") or {}).get("name") \
                            or (se.get("content") or {}).get("body")
                        break
            for ev in (rdata.get("timeline", {}) or {}).get("events", []) or []:
                etype = ev.get("type")
                if etype not in ("m.room.message", "m.room.encrypted"):
                    continue
                content = ev.get("content", {})
                entry = {
                    "room_id": room_id,
                    "room_name": name,
                    "event_id": ev.get("event_id"),
                    "sender": ev.get("sender"),
                    "ts": ev.get("origin_server_ts"),
                }
                if etype == "m.room.encrypted":
                    # Decrypted by the caller (Bot.crypto); the ciphertext
                    # material is carried along so decryption happens once.
                    entry["event_type"] = "m.room.encrypted"
                    entry["raw"] = {
                        "sender_key": content.get("sender_key"),
                        "session_id": content.get("session_id"),
                        "ciphertext": content.get("ciphertext"),
                    }
                else:
                    body = content.get("body")
                    if not isinstance(body, str):
                        continue
                    entry["event_type"] = "m.room.message"
                    entry["msgtype"] = content.get("msgtype", "m.text")
                    entry["body"] = body
                events.append(entry)
        return {
            "next_batch": data.get("next_batch"),
            "events": events,
            "to_device": (data.get("to_device", {}) or {}).get("events", []),
        }

    def get_room_name(self, room_id: str) -> str | None:
        """Current m.room.name of the room (None if it has no name / no access)."""
        try:
            state = self._request(
                "GET", f"{self.hs}/_matrix/client/v3/rooms/"
                       f"{quote(room_id, safe='')}/state/m.room.name/")
        except MatrixError:
            return None
        name = (state or {}).get("name")
        return name if isinstance(name, str) and name else None

    def send_message(self, room_id: str, payload: dict) -> dict:
        for attempt in (1, 2):
            txn = f"whk-{secrets.token_hex(8)}"
            url = f"{self.hs}/_matrix/client/v3/rooms/{quote(room_id)}/send/m.room.message/{txn}"
            try:
                return self._request("PUT", url, payload)
            except MatrixError as exc:
                if (
                    exc.status == 403
                    and attempt != 2
                    and "not in the room" in str(exc.payload).lower()
                ):
                    # Perhaps we were invited meanwhile: try joining once.
                    self.join(room_id)
                    continue
                raise

    def react(self, room_id: str, target_event_id: str, key: str) -> str | None:
        """Add an emoji reaction (``m.annotation`` relation) to ``target_event_id``.

        ``key`` is the emoji (e.g. ``"🔧"``, ``"✅"``). Returns the id of the
        reaction event (needed to redact/remove it later) or ``None``.
        """
        if not key:
            raise ValueError("reaction 'key' (the emoji) is required")
        payload = {"m.relates_to": {
            "rel_type": "m.annotation",
            "event_id": target_event_id,
            "key": key,
        }}
        for attempt in (1, 2):
            txn = f"whk-{secrets.token_hex(8)}"
            url = (f"{self.hs}/_matrix/client/v3/rooms/{quote(room_id)}/"
                   f"send/m.reaction/{txn}")
            try:
                data = self._request("PUT", url, payload)
            except MatrixError as exc:
                if (exc.status == 403 and attempt != 2
                        and "not in the room" in str(exc.payload).lower()):
                    self.join(room_id)
                    continue
                raise
            return data.get("event_id")

    def unreact(self, room_id: str, reaction_event_id: str) -> None:
        """Remove a reaction by redacting the reaction event it produced."""
        txn = f"whk-{secrets.token_hex(8)}"
        url = (f"{self.hs}/_matrix/client/v3/rooms/{quote(room_id, safe='')}/"
               f"redact/{quote(reaction_event_id, safe='')}/{txn}")
        self._request("PUT", url, {})

    def join(self, room_id: str) -> None:
        self._request("POST", f"{self.hs}/_matrix/client/v3/rooms/{quote(room_id)}/join")

    def create_room(self, name: str, invite: str | None, topic: str = "") -> str:
        payload: dict = {
            "name": name,
            # This Synapse (v1.161) only accepts public / private_chat /
            # trusted_private_chat; private_chat = invite-only private room.
            "preset": "private_chat",
            "visibility": "private",
        }
        if topic:
            payload["initial"] = {"type": "m.room.topic", "body": topic}
        if invite:
            payload["invite"] = [invite]
        return self._request("POST", f"{self.hs}/_matrix/client/v3/createRoom", payload)[
            "room_id"
        ]

    # -- internals -------------------------------------------------------------

    def _plain_request(self, method: str, url: str, params: dict | None = None) -> dict:
        resp = self.http.request(method, url, timeout=self.timeout, params=params)
        if resp.status_code >= 400:
            raise MatrixError(resp.status_code, _safe_json(resp))
        return _safe_json(resp) if resp.content else {}

    def _request(self, method: str, url: str, body: dict | None = None,
                 params: dict | None = None, timeout: float | None = None) -> dict:
        self._ensure_token()
        tmo = self.timeout if timeout is None else timeout
        headers = {"Authorization": f"Bearer {self.token}"}
        resp = self.http.request(method, url, json=body, params=params, headers=headers,
                                 timeout=tmo)
        if resp.status_code == 401 and self.token is not None:
            # Token rejected (e.g. after server restart / logout): log in again, retry once.
            self._relogin()
            headers = {"Authorization": f"Bearer {self.token}"}
            resp = self.http.request(method, url, json=body, params=params, headers=headers,
                                     timeout=tmo)
        if resp.status_code >= 400:
            raise MatrixError(resp.status_code, _safe_json(resp))
        return _safe_json(resp) if resp.content else {}


# ---------------------------------------------------------------------------
# Bot: config + client + default room handling
# ---------------------------------------------------------------------------


class Bot:
    def __init__(self, cfg: BotConfig, settings: Settings):
        self.cfg = cfg
        self.settings = settings
        state_dir = settings.state_dir / cfg.name
        state_dir.mkdir(parents=True, exist_ok=True)
        self.client = Matrix(settings.homeserver, cfg.user, cfg.password, state_dir)
        self._crypto: BotCrypto | None = None

    @property
    def name(self) -> str:
        return self.cfg.name

    @property
    def crypto(self) -> BotCrypto:
        """E2E decryption support (lazy; device keys uploaded on first use)."""
        if self._crypto is None:
            self._crypto = BotCrypto(
                self.client, self.client.state_dir,
                on_log=lambda msg: print(f"[crypto:{self.name}] {msg}",
                                        file=sys.stderr))
        return self._crypto

    @property
    def user_id(self) -> str:
        return self.client.user_id

    def room_id_cached_or_pinned(self) -> str | None:
        """Configured or remembered room id, without creating anything."""
        if self.cfg.room_id:
            return self.cfg.room_id
        try:
            saved = (self.client.state_dir / ".room_id").read_text(encoding="utf-8").strip()
            return saved or None
        except OSError:
            return None

    def ensure_room(self) -> str:
        """Room to send to: pinned -> cached -> create (+invite), then cache."""
        existing = self.room_id_cached_or_pinned()
        if existing:
            return existing
        invite = self.settings.invite_user
        if not invite:
            raise MatrixError(0,
                              f"bot '{self.name}' has no room and MATRIX_INVITE_USER "
                              "is not set (cannot create and invite)")
        name = self.cfg.room_name or default_room_name(self.cfg.name)
        room_id = self.client.create_room(name, invite, topic=f"{name} — bot alerts")
        (self.client.state_dir / ".room_id").write_text(room_id + "\n", encoding="utf-8")
        print(f"[{self.name}] created room '{name}' {room_id}, invited {invite}",
              file=sys.stderr)
        return room_id

    def status(self) -> dict:
        return {
            "name": self.name,
            "user": self.user_id,
            "connected": self.client.token is not None,
            "room": self.room_id_cached_or_pinned(),
        }
