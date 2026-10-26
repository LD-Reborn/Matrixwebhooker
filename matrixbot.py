#!/usr/bin/env python3
"""Shared bot model: config loading + per-bot Matrix client.

Used by both ``server.py`` (HTTP API) and ``mcp_server.py`` (MCP tools).

Configuration
-------------
``.env``       shared settings: homeserver, invite user, HTTP bind, OpenCode
               server (``OPENCODE_SERVER_URL`` / ``OPENCODE_SERVER_PASSWORD``)
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
from opencode_api import OpenCodeClient

BASE_DIR = Path(__file__).resolve().parent


class ConfigError(RuntimeError):
    """Raised when the configuration is missing or invalid."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class Action:
    """One OpenCode-server action (``bots.json`` -> ``"action"``).

    Instead of spawning a shell command, the trigger/call talks to the
    long-lived ``opencode serve`` (``OPENCODE_SERVER_URL``): a fresh session
    per message, ``system`` as its instruction entry, ``prompt`` as the user
    turn, and the final assistant text back out (posted to the room / spoken).

    ``prompt``/``system`` may use the placeholders ``{{channel}}``,
    ``{{msg}}`` (last ``context`` messages, newest last), ``{{sender}}``,
    ``{{room}}`` and — for calls — ``{{transcript}}``; values are inserted
    literally (no shell involved). ``model`` is ``providerID/modelID`` (like
    ``opencode run -m``), empty = the server's default. ``template`` names a
    entry from the top-level ``"templates"`` section of ``bots.json``; inline
    fields override it.
    """
    prompt: str
    system: str = ""
    model: str = ""                 # providerID/modelID ('' = server default)
    agent: str = ""                 # opencode agent id ('' = default agent)
    directory: str = ""             # session location ('' = app directory)
    timeout: int = 300              # seconds to wait for the answer
    keep_session: bool = False      # keep the session after a successful run


def action_from(raw, path: str, where: str, default_timeout: int,
                templates: dict) -> Action:
    """Parse/merge one ``"action"`` object (``template`` pulls in a named
    entry from the global ``"templates"`` section; inline keys override)."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: {where}: 'action' must be an object")
    merged: dict = {}
    tname = raw.get("template")
    if tname is not None:
        tname = str(tname).strip()
        tpl = templates.get(tname)
        if tpl is None:
            raise ConfigError(f"{path}: {where}: template '{tname}' is not in "
                              'the top-level "templates" section')
        merged.update(tpl)
    merged.update({k: v for k, v in raw.items() if k != "template"})

    prompt = str(merged.get("prompt") or "").strip()
    if not prompt:
        raise ConfigError(f"{path}: {where}: 'action.prompt' is required "
                          "(via template or inline)")
    try:
        timeout = int(merged.get("timeout", default_timeout))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: {where}: 'action.timeout' must be an "
                          f"integer: {exc}") from exc
    if not 5 <= timeout <= 3600:
        raise ConfigError(f"{path}: {where}: 'action.timeout' must be between "
                          "5 and 3600 seconds")
    return Action(
        prompt=prompt,
        system=str(merged.get("system") or ""),
        model=str(merged.get("model") or "").strip(),
        agent=str(merged.get("agent") or "").strip(),
        directory=str(merged.get("directory") or "").strip(),
        timeout=timeout,
        keep_session=bool(merged.get("keep_session", False)),
    )


@dataclass
class Trigger:
    """Fires when the bot receives a message (not from itself) in one room.

    ``room`` matches the room's display name (case-insensitive) or a room id
    (``!...``) — room ids are the reliable way to match DMs. ``action`` runs
    on the OpenCode server; its ``prompt`` gets ``{{channel}}``, ``{{msg}}``
    (last ``context`` messages, newest last), ``{{sender}}`` and ``{{room}}``
    substituted. The final assistant text is posted back into the room.
    """
    room: str
    action: Action
    context: int = 25               # messages included in {{msg}} (max 100)

    def matches(self, room_id: str, room_name: str | None) -> bool:
        if self.room.startswith("!"):
            return self.room.casefold() == room_id.casefold()
        return room_name is not None and self.room.casefold() == room_name.casefold()


@dataclass
class CallConfig:
    """Answering MatrixRTC (Element Call) calls — ``bots.json`` -> ``"call"``.

    ``action`` runs per heard utterance on the OpenCode server; its final
    assistant text is what the bot speaks back.  On top of the trigger
    placeholders the prompt also gets ``{{transcript}}`` (what was heard);
    ``action.timeout`` bounds one answer (default 90 s).
    ``rooms`` restricts which rooms the bot joins calls in (display names or
    ``!…`` ids); empty means every room the bot is a member of.
    """
    enabled: bool = False
    action: Action | None = None
    context: int = 25
    rooms: list[str] = field(default_factory=list)
    max_seconds: int = 900
    greeting: str = ""
    language: str = ""
    thinking_cue: str = "Einen Moment."   # spoken while the action is still running
    cue_after: float = 2.0                # seconds before that cue
    log_transcripts: bool = True          # post heard + answer into the room
    max_answer_chars: int = 220   # spoken answers are clipped to this (tts is
                                  # ~3.5x the length of what it speaks)
    sorry_line: str = "Das hat zu lange gedauert, frag bitte nochmal."
    error_line: str = "Da ist gerade etwas schiefgegangen, frag bitte nochmal."
    vad_threshold: int = 400        # rms of 16-bit pcm that counts as speech
    echo_factor: float = 2.5        # while we speak, input must be this louder
    barge_in: float = 0.4           # seconds of that before we stop talking
    silence_end: float = 0.9        # seconds of silence closing an utterance
    min_speech: float = 0.35        # shorter blips are dropped
    max_utterance: float = 20.0     # seconds before an utterance is cut off
    # outgoing calls (POST /call, MCP place_call)
    ring_seconds: int = 30          # how long the callee has to pick up
    ring_slot: bool = True          # publish the MSC4143 slot state event
                                    # (current receivers demand it; clients
                                    # too old for it show it as a "Custom
                                    # event" tile — set false for those)
    no_answer_message: str = ""     # posted to the room when nobody picks up
                                    # (empty = post nothing)
    call_message: str = ""          # posted when the bot calls out, so the
                                    # callee can join even if their client
                                    # does not ring; supports {{link}}

    def matches(self, room_id: str, room_name: str | None) -> bool:
        if not self.rooms:
            return True
        for want in self.rooms:
            if want.startswith("!"):
                if want.casefold() == room_id.casefold():
                    return True
            elif room_name and want.casefold() == room_name.casefold():
                return True
        return False


def call_config_from(entry: dict, path: Path, templates: dict) -> CallConfig:
    """Parse the optional ``"call"`` object of a bots.json entry."""
    raw = entry.get("call")
    if raw is None:
        return CallConfig()
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: bots[{entry.get('name')}] 'call' must be an object")
    action = None
    if raw.get("action") is not None:
        action = action_from(raw["action"], str(path),
                             f"bots[{entry.get('name')}].call", 90, templates)
    rooms = raw.get("rooms") or []
    if not isinstance(rooms, list):
        raise ConfigError(f"{path}: 'call.rooms' must be a list of room names/ids")
    try:
        context = int(raw.get("context", 25))
        max_seconds = int(raw.get("max_seconds", 900))
        vad = int(raw.get("vad_threshold", 400))
        silence_end = float(raw.get("silence_end", 0.9))
        echo_factor = float(raw.get("echo_factor", 2.5))
        barge_in = float(raw.get("barge_in", 0.4))
        min_speech = float(raw.get("min_speech", 0.35))
        max_utterance = float(raw.get("max_utterance", 20.0))
        cue_after = float(raw.get("cue_after", 2.0))
        max_answer_chars = int(raw.get("max_answer_chars", 220))
        ring_seconds = int(raw.get("ring_seconds", 30))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: 'call': numeric setting is not a number: {exc}") from exc
    if not 1 <= context <= 100:
        raise ConfigError(f"{path}: 'call.context' must be between 1 and 100")
    if not 30 <= max_seconds <= 7200:
        raise ConfigError(f"{path}: 'call.max_seconds' must be between 30 and 7200")
    if not 5 <= ring_seconds <= 300:
        raise ConfigError(f"{path}: 'call.ring_seconds' must be between 5 and 300")
    return CallConfig(
        enabled=bool(raw.get("enabled", True)),
        action=action,
        context=context,
        rooms=[str(r) for r in rooms],
        max_seconds=max_seconds,
        greeting=str(raw.get("greeting") or ""),
        language=str(raw.get("language") or ""),
        thinking_cue=str(raw.get("thinking_cue", "Einen Moment.")),
        cue_after=cue_after,
        log_transcripts=bool(raw.get("log_transcripts", True)),
        max_answer_chars=max_answer_chars,
        sorry_line=str(raw.get("sorry_line",
                               "Das hat zu lange gedauert, frag bitte nochmal.")),
        error_line=str(raw.get("error_line",
                               "Da ist gerade etwas schiefgegangen, "
                               "frag bitte nochmal.")),
        vad_threshold=vad,
        echo_factor=echo_factor,
        barge_in=barge_in,
        silence_end=silence_end,
        min_speech=min_speech,
        max_utterance=max_utterance,
        ring_seconds=ring_seconds,
        ring_slot=bool(raw.get("ring_slot", True)),
        no_answer_message=str(raw.get("no_answer_message") or ""),
        call_message=str(raw.get("call_message") or ""),
    )


@dataclass
class BotConfig:
    name: str                       # label; also used for the state subdir
    user: str                       # Matrix localpart (no @, no domain)
    password: str
    api_key: str                    # required on /webhook (X-API-Key or Bearer)
    room_id: str | None = None      # pinned room; null = remember/cache/auto-create
    room_name: str | None = None    # name for the auto-created room
    triggers: list[Trigger] = field(default_factory=list)
    call: CallConfig = field(default_factory=CallConfig)


@dataclass
class Settings:
    homeserver: str
    invite_user: str | None         # invited into auto-created rooms
    host: str
    port: int
    state_dir: Path
    bots: list[BotConfig] = field(default_factory=list)
    templates: dict = field(default_factory=dict)  # named actions (bots.json)
    opencode_url: str = "http://127.0.0.1:4096"    # opencode serve base URL
    opencode_password: str = ""                    # Basic-auth password


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

    raw_templates = raw.get("templates") or {}
    if not isinstance(raw_templates, dict):
        raise ConfigError(f'{path}: "templates" must be an object '
                          "(name -> action object)")
    templates: dict[str, dict] = {}
    for tname, traw in raw_templates.items():
        if not isinstance(traw, dict):
            raise ConfigError(f'{path}: templates["{tname}"] must be an object')
        templates[str(tname)] = traw

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
            where = f"bots[{i}] ('{name}').triggers[{j}]"
            if t.get("command") is not None or t.get("commands") is not None:
                raise ConfigError(
                    f"{path}: {where}: 'command' was replaced by 'action' — "
                    "the trigger now runs on the OpenCode server "
                    "(see bots.example.json)")
            if not isinstance(t.get("action"), dict):
                raise ConfigError(f"{path}: {where}: 'action' must be an object "
                                  "(optionally {\"template\": \"...\"})")
            action = action_from(t["action"], str(path), where, 300, templates)
            try:
                context = int(t.get("context", 25))
            except (TypeError, ValueError):
                raise ConfigError(f"{path}: {where}: "
                                  "'context' must be an integer")
            if not 1 <= context <= 100:
                raise ConfigError(f"{path}: {where}: "
                                  "'context' must be between 1 and 100")
            triggers.append(Trigger(room=troom, action=action, context=context))
        bots.append(BotConfig(
            name=name,
            user=user,
            password=password,
            api_key=api_key,
            room_id=room_id,
            room_name=str(entry.get("room_name") or "").strip() or None,
            triggers=triggers,
            call=call_config_from(entry, path, templates),
        ))

    return Settings(
        homeserver=homeserver,
        invite_user=invite_user,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=port,
        state_dir=state_dir,
        bots=bots,
        templates=templates,
        opencode_url=os.environ.get("OPENCODE_SERVER_URL", "").strip().rstrip("/") \
            or "http://127.0.0.1:4096",
        opencode_password=os.environ.get("OPENCODE_SERVER_PASSWORD", "").strip(),
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


#: Room-state event types that tell us a MatrixRTC call is running.  Element
#: Call has used three shapes so far: the MSC3401 session membership (state
#: key = user id), the newer MSC4143 MatrixRTC membership (sticky, state key =
#: member id) and, oldest, the im.element.call.member widget convention.  The
#: slot event is watched too: closing it ends a call.
CALL_STATE_TYPES = (
    "org.matrix.msc3401.call.member",
    "org.matrix.msc4143.rtc.member",
    "im.element.call.member",
    "org.matrix.msc4143.rtc.slot",
)


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
        """Last ``limit`` room events (m.room.message + m.reaction), oldest first.

        Encrypted events are decrypted when ``crypto`` is given; events that
        cannot be decrypted are skipped. Reactions appear as
        ``{"type": "m.reaction", "reacts_to": <event_id>, "key": <emoji>}``.
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
            elif etype == "m.reaction":
                rel = (ev.get("content", {}) or {}).get("m.relates_to") or {}
                if rel.get("rel_type") != "m.annotation":
                    continue
                key = rel.get("key")
                if not isinstance(key, str):
                    continue
                out.append({
                    "event_id": ev.get("event_id"),
                    "sender": ev.get("sender"),
                    "ts": ev.get("origin_server_ts"),
                    "type": "m.reaction",
                    "reacts_to": rel.get("event_id"),
                    "key": key,
                })
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
                "type": "m.room.message",
            })
        return out

    def sync(self, since: str | None = None, timeout_ms: int = 30000) -> dict:
        """One (long-polling) /sync round trip.

        Returns ``{"next_batch": ..., "events": [...], "state": [...],
        "to_device": [...]}``. Events are the *new* m.room.message /
        m.room.encrypted events of the joined rooms (oldest first, per room),
        each with ``room_id``, ``room_name``, ``event_id``, ``sender``, ``ts``
        and, for plaintext ones, ``msgtype``/``body``; encrypted ones carry
        ``raw`` (Megolm material) for decryption by ``Bot.crypto``. ``state``
        holds *changed* room state of interest (room name plus the MatrixRTC
        call-membership events, see CALL_STATE_TYPES — those are picked out of
        the timeline, because /sync only sends state deltas when the timeline
        is limited). ``to_device`` holds the
        raw to-device events (Olm/Megolm key material). A call without
        ``since`` is a snapshot: its events are historical and the caller
        must not act on them.
        """
        flt = json.dumps(
            {"room": {
                "timeline": {"types": ["m.room.message", "m.room.encrypted",
                                       *CALL_STATE_TYPES], "limit": 100},
                "state": {"types": ["m.room.name", "m.room.canonical_alias",
                                    *CALL_STATE_TYPES]},
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
        state: list[dict] = []
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
            seen_state: set[tuple] = set()
            for ev in (rdata.get("timeline", {}) or {}).get("events", []) or []:
                etype = ev.get("type")
                if etype in CALL_STATE_TYPES:
                    # State events also arrive in the timeline, and that is the
                    # only reliable place to see them: /sync sends state deltas
                    # only when the timeline comes back "limited".
                    entry = self._state_entry(room_id, name, ev)
                    seen_state.add((entry["type"], entry["state_key"], entry["event_id"]))
                    state.append(entry)
                    continue
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
            for se in ((rdata.get("state", {}) or {}).get("events", [])) or []:
                if se.get("type") not in CALL_STATE_TYPES:
                    continue
                entry = self._state_entry(room_id, name, se)
                if (entry["type"], entry["state_key"], entry["event_id"]) in seen_state:
                    continue
                state.append(entry)
        return {
            "next_batch": data.get("next_batch"),
            "events": events,
            "state": state,
            "to_device": (data.get("to_device", {}) or {}).get("events", []),
        }

    @staticmethod
    def _state_entry(room_id: str, name: str | None, ev: dict) -> dict:
        return {
            "room_id": room_id,
            "room_name": name,
            "type": ev.get("type"),
            "state_key": ev.get("state_key"),
            "event_id": ev.get("event_id"),
            "sender": ev.get("sender"),
            "ts": ev.get("origin_server_ts"),
            "content": ev.get("content") or {},
        }

    # -- room state (MatrixRTC calls) ------------------------------------------

    def send_state(self, room_id: str, event_type: str, state_key: str,
                   content: dict) -> dict:
        """PUT a room state event (used to publish our call membership)."""
        url = (f"{self.hs}/_matrix/client/v3/rooms/{quote(room_id, safe='')}/state/"
               f"{quote(event_type, safe='')}/{quote(state_key, safe='')}")
        return self._request("PUT", url, content)

    def room_state(self, room_id: str, types: tuple[str, ...] | None = None) -> list[dict]:
        """Current room state events (optionally only the given types)."""
        events = self._request("GET", f"{self.hs}/_matrix/client/v3/rooms/"
                                      f"{quote(room_id, safe='')}/state")
        if not isinstance(events, list):
            return []
        if types is None:
            return events
        return [ev for ev in events if ev.get("type") in types]

    # -- MatrixRTC / LiveKit -----------------------------------------------------

    def rtc_transports(self) -> list[dict]:
        """LiveKit transports the homeserver advertises (MSC4143 endpoint,
        falling back to the .well-known rtc_foci)."""
        try:
            data = self._request(
                "GET", f"{self.hs}/_matrix/client/unstable/org.matrix.msc4143/rtc/transports")
            transports = [t for t in (data.get("rtc_transports") or [])
                          if isinstance(t, dict)]
            if transports:
                return transports
        except MatrixError:
            pass
        try:
            well = self._plain_request(
                "GET", f"{self.hs}/.well-known/matrix/client")
        except MatrixError:
            return []
        foci = (well.get("org.matrix.msc4143.rtc_foci")
                or well.get("org.matrix.msc3266.rtc_foci") or [])
        return [f for f in foci if isinstance(f, dict)]

    def openid_token(self) -> dict:
        """Short-lived OpenID token the LiveKit JWT service validates."""
        return self._request(
            "POST", f"{self.hs}/_matrix/client/v3/user/"
                    f"{quote(self.user_id, safe='')}/openid/request_token",
            {"access_token": self.token})

    def livekit_token(self, room_id: str, slot_id: str = "m.call#ROOM",
                      member_id: str | None = None) -> dict:
        """-> ``{"url": "wss://…/sfu", "jwt": "…"}`` for one call slot.

        With a ``member_id`` we ask the JWT service the MSC4195 way
        (``/get_token``), so the LiveKit identity is that member id.  Without
        one we use the older ``/sfu/get``, whose identity is
        ``userId:deviceId`` — the form current Element Call clients publish as
        ``membershipID`` and use to match LiveKit participants to call
        memberships.  Join with the wrong identity and the other side never
        hears us: their client has no membership to attach our audio to.
        """
        transports = self.rtc_transports()
        service = next((t.get("livekit_service_url") for t in transports
                        if t.get("livekit_service_url")), None)
        sfu = next((t.get("url") for t in transports if t.get("url")), None)
        if not service and not sfu:
            raise MatrixError(0, "homeserver advertises no livekit transport "
                                 "(.well-known org.matrix.msc4143.rtc_foci)")
        if not service:
            # SFU url only: the homeserver itself issues the token (MSC4195).
            data = self._request(
                "POST", f"{self.hs}/_matrix/client/unstable/io.element.msc4195"
                        f"/rtc/livekit/get_token",
                {"url": sfu, "room_id": room_id, "slot_id": slot_id,
                 "member_id": member_id or f"{self.user_id}:{self.device_id}"})
            return {"url": sfu, "jwt": data.get("jwt", "")}
        oid = self.openid_token()
        if member_id is None:
            body = {"room": room_id, "openid_token": oid,
                    "device_id": self.device_id or ""}
            paths = ("/sfu/get", "/get_token")
        else:
            body = {"room_id": room_id, "slot_id": slot_id, "openid_token": oid,
                    "member": {"id": member_id, "claimed_user_id": self.user_id,
                               "claimed_device_id": self.device_id or ""}}
            paths = ("/get_token", "/sfu/get")
        last: MatrixError | None = None
        for path in paths:
            try:
                data = self._request("POST", service.rstrip("/") + path, body)
            except MatrixError as exc:
                last = exc
                continue
            if data.get("jwt"):
                return {"url": data.get("url") or sfu or "", "jwt": data["jwt"]}
        raise last or MatrixError(0, f"livekit token request failed at {service}")

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

    def send_event(self, room_id: str, event_type: str, content: dict) -> dict:
        """Send a room event of an arbitrary type (e.g. the MSC4075
        ``org.matrix.msc4075.rtc.notification`` that makes clients ring)."""
        txn = f"whk-{secrets.token_hex(8)}"
        url = (f"{self.hs}/_matrix/client/v3/rooms/{quote(room_id, safe='')}/send/"
               f"{quote(event_type, safe='')}/{txn}")
        for attempt in (1, 2):
            try:
                return self._request("PUT", url, content)
            except MatrixError as exc:
                if (exc.status == 403 and attempt != 2
                        and "not in the room" in str(exc.payload).lower()):
                    self.join(room_id)
                    continue
                raise
        return {}

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

    def joined_members(self, room_id: str) -> list[str]:
        """User ids of the room's joined members (needs auth in the room)."""
        data = self._request("GET", f"{self.hs}/_matrix/client/v3/rooms/"
                                    f"{quote(room_id, safe='')}/joined_members")
        return list((data.get("joined") or {}).keys())

    def send_to_device(self, event_type: str, messages: dict) -> dict:
        """Send a to-device event.  ``messages`` maps user id -> device id
        (``"*"`` = every session) -> content — e.g. the MSC4184
        ``m.call.notify`` that makes an Element Call client ring."""
        txn = f"whk-{secrets.token_hex(8)}"
        url = (f"{self.hs}/_matrix/client/v3/sendToDevice/"
               f"{quote(event_type, safe='')}/{txn}")
        return self._request("PUT", url, {"messages": messages})

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


class EffortStore:
    """Per-room reasoning-effort override, set with the room's ``/effort``.

    Maps room id (casefolded) to an OpenCode model variant (``low`` /
    ``medium`` / ``high`` / ``xhigh``); no entry means the model's default
    effort.  Persisted in the bot's state dir so a restart keeps every
    room's setting.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._levels: dict[str, str] = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._levels = {str(k).casefold(): str(v)
                                for k, v in data.items()}
        except (OSError, ValueError):
            pass

    def get(self, room_id: str) -> str:
        """The room's model variant, or ``""`` for the model default."""
        with self._lock:
            return self._levels.get(room_id.casefold(), "")

    def set(self, room_id: str, variant: str) -> None:
        """Set the room's variant (``""`` resets it to the model default)."""
        key = room_id.casefold()
        with self._lock:
            if variant:
                self._levels[key] = variant
            else:
                self._levels.pop(key, None)
            snapshot = dict(self._levels)
        try:
            self._path.write_text(
                json.dumps(snapshot, indent=1, sort_keys=True),
                encoding="utf-8")
        except OSError as exc:
            print(f"[effort] saving {self._path} failed: {exc}",
                  file=sys.stderr)


class Bot:
    def __init__(self, cfg: BotConfig, settings: Settings):
        self.cfg = cfg
        self.settings = settings
        state_dir = settings.state_dir / cfg.name
        state_dir.mkdir(parents=True, exist_ok=True)
        self.client = Matrix(settings.homeserver, cfg.user, cfg.password, state_dir)
        #: per-room /effort overrides (room id -> OpenCode model variant)
        self.effort = EffortStore(state_dir / "effort.json")
        self._crypto: BotCrypto | None = None
        self._opencode: OpenCodeClient | None = None
        #: set by server.py when the bot has "call" enabled (calls.CallManager)
        self.calls = None

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
    def opencode(self) -> OpenCodeClient:
        """OpenCode server client (lazy; one per bot, thread-safe)."""
        if self._opencode is None:
            self._opencode = OpenCodeClient(self.settings.opencode_url,
                                            self.settings.opencode_password)
        return self._opencode

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
        out = {
            "name": self.name,
            "user": self.user_id,
            "connected": self.client.token is not None,
            "room": self.room_id_cached_or_pinned(),
        }
        if self.calls is not None:
            out["call_enabled"] = self.cfg.call.enabled
            out["calls"] = self.calls.status()
        return out
