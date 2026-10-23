#!/usr/bin/env python3
"""Incoming-message watcher, per-bot triggers and reply waiting.

Runs inside ``server.py`` (the long-lived process): one ``MessageWatcher``
thread per bot long-polls Matrix ``/sync`` and, for every room message the
bot receives — any channel it is in, including DMs — in this order:

  1. messages sent by the bot itself are ignored (never a trigger);
  2. a bare "stop" keyword (``stop`` / ``/stop`` / ``STOP`` / ``STOP!!!`` —
     starts with "stop" and contains no whitespace) halts the current
     execution for that room: every OpenCode session the running trigger
     started is interrupted (``opencode_api``), any pending waiters (a model
     blocked in ``wait_for_reply``) are released, and every wrench reaction
     added while it was handled is removed again.  The stop command is never
     delivered to a waiter or fired as a trigger.
  2b. ``/effort`` (list the reasoning-effort levels of the room) and
      ``/effort <default|low|mid|high|xhigh>`` (set it for the room, persisted
      in the bot's state dir) — the room's level becomes the model
      ``variant`` of every action run started in that room afterwards.
  2c. ``/btw <text>`` interjects into the action(s) currently running for
      that room (OpenCode inbox ``steer`` delivery) instead of starting a
      new run or feeding a waiter.
  3. if a client is waiting for a reply in that room (``POST /wait``, or
     the MCP ``wait_for_reply`` tool), the message is handed to the oldest
     waiter instead of running any triggers.  While a trigger run for that
     room is still in flight, the delivered message is additionally marked
     with the wrench reaction (see below);
  4. otherwise every trigger of the bot whose room matches is fired in a
     background thread: its ``action`` runs on the OpenCode server
     (``opencode_api.py``) — a fresh session per message with the
     ``{{channel}}``, ``{{msg}}`` (last ``context`` messages, newest last),
     ``{{sender}}`` and ``{{room}}`` placeholders substituted into the
     prompt — and the final assistant text is posted back into the room.

Wrench feedback (🔧):
    Whenever a message is *handled* — a trigger for its room is fired, or it
    is delivered to a model that is already waiting for a reply in that room
    while that trigger run is still in flight — the bot reacts to it with the
    wrench emoji ``🔧``.  As soon as handling of that message is finished
    (the trigger action ends, or the room's "stop" command is received),
    the reaction is removed again (the reaction event is redacted), so you
    can tell at a glance which messages the bot is still working on.

``mcp_server.py`` deliberately runs no watcher of its own: its
``wait_for_reply`` tool talks to this server, so waits and trigger
suppression always live in exactly one process (no double-firing).
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path

from crypto import CryptoError
from matrixbot import BASE_DIR, Bot, MatrixError
from opencode_api import OpenCodeError

#: Hard cap (seconds) for POST /wait and the MCP wait_for_reply tool.
MAX_WAIT_SECONDS = 600

#: The reaction the bot adds to a message while it is being handled, and
#: removes again as soon as handling of that message is over.
WRENCH_REACTION = "🔧"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def is_stop_command(body: str | None) -> bool:
    """True for a bare ``stop`` keyword — and nothing else.

    Matches exactly the word ``stop`` (ignoring case, an optional leading
    ``/``, and trailing punctuation like ``!?.``), with no whitespace:
    ``stop`` / ``/stop`` / ``STOP`` / ``STOP!!!`` / ``sTop?`` all match, but
    a sentence such as ``stop ollama.service`` and a different word such as
    ``stopped`` do not.
    """
    if not body:
        return False
    text = body.strip()
    if not text or re.search(r"\s", text):
        return False
    return text.casefold().lstrip("/").rstrip("!?.…:;") == "stop"


#: the reasoning-effort levels ``/effort`` accepts, mapped to the OpenCode
#: model variants the model catalog uses ("mid" is the friendly alias of
#: "medium"; "default" clears the room's override = the model's own default)
EFFORT_LEVELS = ("default", "low", "mid", "high", "xhigh")
EFFORT_VARIANTS = {"low": "low", "mid": "medium", "medium": "medium",
                   "high": "high", "xhigh": "xhigh"}

_BTW_RE = re.compile(r"^/btw\b[\s,]*(.*)$", re.IGNORECASE | re.DOTALL)


def parse_effort_command(body: str | None) -> tuple[bool, str]:
    """True for ``/effort`` or ``/effort <level>`` — and nothing else.

    The whole message must be the command (leading ``/`` required, case
    ignored); the optional argument is returned stripped and lowercased,
    ``""`` for the bare list form.
    """
    if not body:
        return False, ""
    text = body.strip()
    if not text.startswith("/"):
        return False, ""
    head, _, rest = text.partition(" ")
    if head.casefold().lstrip("/").rstrip("!?.…:;") != "effort":
        return False, ""
    return True, rest.strip().casefold().rstrip("!?.…:;")


def parse_btw_command(body: str | None) -> tuple[bool, str]:
    """True for ``/btw <text>`` — an interjection for a running action.

    The leading ``/`` is required: a plain "btw …" in chat stays a normal
    message.  Returns the interjection text (may be empty for a bare
    ``/btw``, which the caller answers with the usage note).
    """
    if not body:
        return False, ""
    m = _BTW_RE.match(body.strip())
    if m is None:
        return False, ""
    return True, m.group(1).strip()


# ---------------------------------------------------------------------------
# Waiting for a reply (consumers of incoming messages, ahead of triggers)
# ---------------------------------------------------------------------------


class _Waiter:
    __slots__ = ("event", "message", "stopped")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.message: dict | None = None
        self.stopped = False


class WaitRegistry:
    """Pending reply waiters, one FIFO queue per (bot, room)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: dict[tuple[str, str], deque[_Waiter]] = {}

    @staticmethod
    def _key(bot_name: str, room_id: str) -> tuple[str, str]:
        return (bot_name.casefold(), room_id.casefold())

    def register(self, bot_name: str, room_id: str) -> _Waiter:
        key = self._key(bot_name, room_id)
        waiter = _Waiter()
        with self._lock:
            self._queues.setdefault(key, deque()).append(waiter)
        return waiter

    def deliver(self, bot_name: str, room_id: str, message: dict) -> bool:
        """Hand ``message`` to the oldest waiting client of that bot+room.

        Returns True if a waiter consumed it (callers must NOT run triggers).
        """
        key = self._key(bot_name, room_id)
        with self._lock:
            queue = self._queues.get(key)
            waiter = queue.popleft() if queue else None
        if waiter is None:
            return False
        if waiter.message is None:
            waiter.message = message
            waiter.event.set()
        return True

    def cancel(self, bot_name: str, room_id: str) -> int:
        """Release and drop every pending waiter of that bot+room (stop).

        Each waiter is unblocked immediately with ``stopped = True`` and
        ``message = None`` so the waiting ``/wait`` / ``wait_for_reply``
        client returns right away. Returns how many waiters were released.
        """
        key = self._key(bot_name, room_id)
        with self._lock:
            queue = self._queues.get(key)
            waiters = list(queue) if queue else []
            if queue:
                queue.clear()
        for w in waiters:
            if w.message is None:
                w.stopped = True
                w.event.set()
        return len(waiters)


# ---------------------------------------------------------------------------
# Trigger rendering
# ---------------------------------------------------------------------------


def format_history(messages: list[dict]) -> str:
    """The ``{{msg}}`` value: one line per message, newest last."""
    lines = []
    for m in messages:
        body = " ".join(str(m.get("body", "")).split())
        sender = m.get("sender") or "?"
        ts = m.get("ts")
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts / 1000)) if ts else "?"
        lines.append(f"[{when}] {sender}: {body}")
    return "\n".join(lines)


def render_text(template: str, values: dict[str, str]) -> str:
    """Replace {{placeholder}} with the values, inserted literally.

    (No shell is involved any more — the old command lines needed
    shell-quoting, an action prompt does not.)
    """
    out = template
    for key, value in values.items():
        out = out.replace("{{%s}}" % key, value)
    return out


# ---------------------------------------------------------------------------
# Per-bot watcher
# ---------------------------------------------------------------------------


class _Session:
    """One in-flight run of a bot's triggers in a single room: the OpenCode
    session ids started for it, the wrench-reaction events added on the
    room's behalf while it runs, and its state.  Wrench removal happens
    exactly once (whichever of the run's end / a stop claim wins it via
    ``cleaned``)."""

    __slots__ = ("room_id", "session_ids", "reactions", "cancelled", "cleaned")

    def __init__(self, room_id: str) -> None:
        self.room_id = room_id
        self.session_ids: list[str] = []
        self.reactions: list[str] = []
        self.cancelled = False
        self.cleaned = False


class MessageWatcher:
    """Watches one bot's incoming messages and fires its triggers."""

    SYNC_TIMEOUT_MS = 30000

    def __init__(self, bot: Bot, waiters: WaitRegistry):
        self.bot = bot
        self.waiters = waiters
        self._since: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # room_id -> display name. Incremental /sync responses carry no
        # unchanged state, so names learned from the initial snapshot (or
        # the state API) are kept here for trigger matching.
        self._room_names: dict[str, str] = {}
        # In-flight runs (casefolded room_id -> active sessions).  Guarded by
        # _session_lock; shared with the stop path and the wrench bookkeeping.
        self._session_lock = threading.Lock()
        self._sessions: dict[str, list[_Session]] = {}
        # Events that arrived E2E-encrypted but could not be decrypted (the
        # sender's group key has not been shared with us yet).  Keyed by
        # "room_id|sender_key|session_id"; each entry keeps the undecryptable
        # events plus the state of the room-key requests we have sent.  On
        # every sync round — as soon as the key lands via to-device — the
        # events are decrypted again and flow through the normal pipeline.
        # Persisted (state dir) so a restart does not forget them.
        self._key_pending: dict[str, dict] = {}
        self._pending_file = Path(self.bot.client.state_dir) / "key_pending.json"

    def _load_key_pending(self) -> None:
        try:
            data = json.loads(self._pending_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._key_pending = data.get("entries") or {}
        if self._key_pending:
            n = sum(len(e.get("events") or []) for e in self._key_pending.values())
            _log(f"{self.tag} restored {n} pending (undecryptable) event(s)")

    def _save_key_pending(self) -> None:
        try:
            payload = {"ts": int(time.time()), "entries": self._key_pending}
            self._pending_file.write_text(
                json.dumps(payload, indent=1, sort_keys=True),
                encoding="utf-8")
        except OSError as exc:
            _log(f"{self.tag} saving pending keys failed: {exc}")

    def _register_pending(self, ev: dict, raw: dict) -> None:
        """Remember an undecryptable event and (re)request its key once."""
        room_id = str(ev.get("room_id") or "")
        sender_key = str(raw.get("sender_key") or "")
        session_id = str(raw.get("session_id") or "")
        if not (room_id and sender_key and session_id):
            return
        key = f"{room_id}|{sender_key}|{session_id}"
        entry = self._key_pending.setdefault(key, {
            "user": ev.get("sender"),
            "requests": 0,
            "last_req": 0.0,
            "events": [],
        })
        for e in entry["events"]:
            if e.get("event_id") == ev.get("event_id"):
                return  # already queued
        entry["events"].append({
            "event_id": ev.get("event_id"),
            "sender": ev.get("sender"),
            "room_name": ev.get("room_name"),
            "ts": ev.get("ts"),
            "raw": raw,
        })
        self._try_request_key(entry, room_id, sender_key, session_id)
        self._save_key_pending()

    def _try_request_key(self, entry: dict, room_id: str,
                         sender_key: str, session_id: str) -> bool:
        """Send (or re-send, throttled) a room-key request to the sender."""
        user = entry.get("user")
        if not user or user == self.bot.user_id:
            return False
        now = time.time()
        if entry["requests"] >= 4:
            return False
        if entry["requests"] and now - entry["last_req"] < 90.0:
            return False
        recipient = "*"
        if self.bot.crypto.find_device_with_curve(user, sender_key):
            recipient = (self.bot.crypto.find_device_with_curve(
                user, sender_key) or "*")
        try:
            self.bot.crypto.request_room_key(
                user, recipient, room_id, sender_key, session_id)
        except MatrixError as exc:
            _log(f"{self.tag} key request failed: {exc}")
            return False
        except Exception as exc:  # noqa: BLE001
            _log(f"{self.tag} key request failed: {exc!r}")
            return False
        entry["requests"] += 1
        entry["last_req"] = now
        return True

    def _retry_pending_keys(self) -> list[dict]:
        """Re-attempt the pending events: the key may have arrived via
        to-device since they failed.  Returns decrypted events, oldest per
        group first, ready for the normal pipeline."""
        recovered: list[dict] = []
        if not self._key_pending:
            return recovered
        now = time.time()
        changed = False
        for key in list(self._key_pending):
            room_id, sender_key, session_id = key.split("|", 2)
            entry = self._key_pending[key]
            left: list[dict] = []
            had = len(entry.get("events") or [])
            for meta in entry.get("events") or []:
                try:
                    content = self.bot.crypto.decrypt_room_content(
                        meta.get("raw") or {})
                except CryptoError as exc:
                    if "no megolm session" in str(exc):
                        left.append(meta)      # key still missing: keep
                    # else (e.g. index before imported start): give up on it
                    continue
                except Exception:  # noqa: BLE001
                    left.append(meta)
                    continue
                if not isinstance(content.get("body"), str):
                    continue
                recovered.append({
                    "room_id": room_id,
                    "room_name": (self._room_names.get(room_id)
                                  or meta.get("room_name")),
                    "event_id": meta.get("event_id"),
                    "sender": meta.get("sender"),
                    "ts": meta.get("ts"),
                    "event_type": "m.room.message",
                    "body": content["body"],
                    "msgtype": content.get("msgtype", "m.text"),
                })
            if left:
                entry["events"] = left
                if now - entry["last_req"] >= 120.0 and \
                        self._try_request_key(entry, room_id, sender_key,
                                              session_id):
                    changed = True
            else:
                del self._key_pending[key]
            if len(left) != had:
                changed = True
        if recovered:
            _log(f"{self.tag} decrypted {len(recovered)} previously stuck "
                 "message(s) after room-key import")
        if changed:
            self._save_key_pending()
        return recovered

    def start(self) -> None:
        self._load_key_pending()
        calls = getattr(self.bot, "calls", None)
        if calls is not None:
            calls.prewarm()      # greeting + cue, so a call starts talking at once
            # a call may already be running (we restarted mid-call)
            threading.Thread(target=calls.poll_rooms,
                             name=f"callpoll-{self.bot.name}", daemon=True).start()
        self._thread = threading.Thread(target=self._run,
                                        name=f"watch-{self.bot.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def tag(self) -> str:
        return f"[watch:{self.bot.name}]"

    def _name_for(self, room_id: str, from_sync: str | None) -> str | None:
        if from_sync:
            self._room_names[room_id] = from_sync
            return from_sync
        name = self._room_names.get(room_id)
        if name:
            return name
        try:
            name = self.bot.client.get_room_name(room_id)
        except MatrixError as exc:
            _log(f"{self.tag} room name lookup failed for {room_id}: {exc}")
            name = None
        if name:
            self._room_names[room_id] = name
        return name

    # -- main loop ------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except MatrixError as exc:
                if exc.status in (400, 403):
                    # Stale sync token / reset state: resync from scratch.
                    self._since = None
                    _log(f"{self.tag} resyncing next round after: {exc}")
                else:
                    _log(f"{self.tag} sync failed: {exc}")
                self._stop.wait(5)
            except Exception as exc:  # noqa: BLE001 - the watcher must survive
                _log(f"{self.tag} unexpected error: {exc!r}")
                self._stop.wait(5)

    def _poll_once(self) -> None:
        was_first = self._since is None
        result = self.bot.client.sync(since=self._since,
                                      timeout_ms=self.SYNC_TIMEOUT_MS)
        self._since = result.get("next_batch") or self._since
        if was_first:
            # Snapshot: learn room names, but never act on history.
            for ev in result["events"]:
                if ev.get("room_name"):
                    self._room_names[ev["room_id"]] = ev["room_name"]
            # Key material delivered in the snapshot can still be useful
            # later (group keys shared before we logged in): import it.
            to_device = result.get("to_device") or []
            if to_device:
                self._ingest_todevice(to_device)
            # Stuck (undecryptable) events restored from disk may decrypt now
            # that carried-over state covers them; they are not history.
            for ev in self._retry_pending_keys():
                self._handle(ev)
            _log(f"{self.tag} primed ({len(result['events'])} historical "
                 f"message(s) skipped)")
            return
        to_device = result.get("to_device") or []
        if to_device:
            self._ingest_todevice(to_device)
        for ev in result.get("state") or []:
            self._on_state(ev)
        # The key failed events were waiting for may have just arrived —
        # retry them before processing the fresh timeline events.
        events = self._retry_pending_keys() + self._decrypt_events(
            result.get("events") or [])
        if not events:
            return
        _log(f"{self.tag} +{len(events)} message(s)")
        for ev in events:
            self._handle(ev)

    def _on_state(self, ev: dict) -> None:
        """Room state changes: hand call membership events to the CallManager."""
        calls = getattr(self.bot, "calls", None)
        if calls is None:
            return
        try:
            calls.on_state_event(ev)
        except Exception as exc:  # noqa: BLE001 - state handling must not kill the loop
            _log(f"{self.tag} call state handling failed: {exc!r}")

    def _ingest_todevice(self, events: list[dict]) -> None:
        """Feed to-device (Olm/Megolm) events into the bot's crypto layer.

        Best-effort: a failure must never kill the watcher loop.
        """
        try:
            self.bot.crypto.handle_todevice(events)
        except Exception as exc:  # noqa: BLE001
            _log(f"{self.tag} crypto: to-device ingest failed: {exc!r}")

    def _decrypt_events(self, events: list[dict]) -> list[dict]:
        """Decrypt m.room.encrypted timeline events; drop the ones that
        cannot be decrypted (they would be unusable to the model)."""
        out: list[dict] = []
        for ev in events:
            if ev.get("event_type") != "m.room.encrypted":
                out.append(ev)
                continue
            raw = ev.get("raw") or {}
            try:
                content = self.bot.crypto.decrypt_room_content(raw)
            except CryptoError as exc:
                _log(f"{self.tag} {ev.get('room_id')} {ev.get('event_id') or ''}: "
                     f"not decrypted ({exc}); queued for key request")
                # The sender's client may still share the group key once asked
                # (m.room_key_request).  Queue the event so it is decrypted —
                # and handled — as soon as the key arrives.
                self._register_pending(ev, raw)
                continue
            except Exception as exc:  # noqa: BLE001
                _log(f"{self.tag} {ev.get('room_id')} {ev.get('event_id') or ''}: "
                     f"not decrypted ({exc}); skipping")
                continue
            if not isinstance(content.get("body"), str):
                # Decrypted fine but not a plain text message (poll, notice
                # without body, ...): the pipeline expects a body, so skip.
                continue
            out.append({**ev,
                        "event_type": "m.room.message",
                        "body": content["body"],
                        "msgtype": content.get("msgtype", "m.text")})
        return out

    # -- wrench reactions -----------------------------------------------------

    def _react_on(self, room_id: str, event_id: str) -> str | None:
        """Add the wrench emoji to ``event_id``.

        Returns the reaction's own event id (for redacting it later), or None
        if there was nothing to react to / the reaction failed.
        """
        if not event_id:
            return None
        try:
            return self.bot.client.react(room_id, event_id, WRENCH_REACTION)
        except MatrixError as exc:
            _log(f"{self.tag} adding wrench to {event_id} failed: {exc}")
            return None

    def _unreact(self, room_id: str, reaction_id: str) -> None:
        try:
            self.bot.client.unreact(room_id, reaction_id)
        except MatrixError as exc:
            _log(f"{self.tag} removing wrench ({reaction_id}) failed: {exc}")

    def _clean_up(self, sess: _Session) -> None:
        """Atomically claim this session's wrench removal (happens exactly once)
        and redact every wrench we added for it."""
        with self._session_lock:
            if sess.cleaned:
                return
            sess.cleaned = True
            my_reactions = list(sess.reactions)
        for rid in my_reactions:
            self._unreact(sess.room_id, rid)
        if my_reactions:
            _log(f"{self.tag} wrench removed from {len(my_reactions)} message(s)")

    # -- stop keyword ---------------------------------------------------------

    def _interrupt_sessions(self, sess: _Session) -> None:
        """Interrupt every OpenCode session this run started (best-effort)."""
        with self._session_lock:
            ids = list(sess.session_ids)
        if not ids:
            return
        for sid in ids:
            try:
                self.bot.opencode.interrupt(sid)
            except OpenCodeError as exc:
                _log(f"{self.tag} interrupting {sid} failed: {exc}")

    def _send_note(self, room_id: str, message: str) -> None:
        """Post a short command-acknowledgment text into the room."""
        try:
            self.bot.client.send_message(room_id,
                                         {"msgtype": "m.text", "body": message})
        except MatrixError as exc:
            _log(f"{self.tag} sending the command reply failed: {exc}")

    def _stop_room(self, room_id: str, where: str) -> None:
        """Handle a bare "stop" in ``room_id``: interrupt the run, release
        waiters, leave any call and clean up the wrench reactions."""
        key = room_id.casefold()
        with self._session_lock:
            active = list(self._sessions.get(key, []))
        for sess in active:
            sess.cancelled = True
            self._interrupt_sessions(sess)
            self._clean_up(sess)
        cancelled = self.waiters.cancel(self.bot.name, room_id)
        calls = getattr(self.bot, "calls", None)
        hung_up = calls is not None and room_id in [s["room"] for s in calls.status()]
        if hung_up:
            calls.stop_room(room_id, "stop command")
        if active:
            ack = "⏹ Stopped."
        elif cancelled:
            ack = "⏹ Stop sent (waiting model released)."
        elif hung_up:
            ack = "⏹ Left the call."
        else:
            ack = "⏹ Nothing was running."
        self._send_note(room_id, ack)
        _log(f"{self.tag} {where} -> stopped: {len(active)} run(s), "
             f"{cancelled} waiter(s) released, call left={hung_up}")

    # -- /effort command --------------------------------------------------------

    def _effort_room(self, room_id: str, level: str) -> None:
        """``/effort`` lists the room's levels, ``/effort <level>`` sets one.

        The setting is per room and persists across restarts (the bot's
        state dir); every action run started in the room afterwards gets it
        as its model ``variant``.
        """
        store = self.bot.effort
        levels = ", ".join(EFFORT_LEVELS)
        if not level:
            current = store.get(room_id) or "default"
            self._send_note(room_id,
                            f"⚙️ Effort in this room: {current}. "
                            f"Levels: {levels} — set with /effort <level>.")
            return
        if level == "default":
            store.set(room_id, "")
            self._send_note(room_id, "⚙️ Effort reset to the model default.")
        elif level in EFFORT_VARIANTS:
            store.set(room_id, EFFORT_VARIANTS[level])
            self._send_note(room_id, f"⚙️ Effort in this room: {level}.")
        else:
            self._send_note(room_id, f"⚙️ Unknown effort level '{level}'. "
                                     f"Levels: {levels}.")
        _log(f"{self.tag} /effort {level or '(list)'} for {room_id} "
             f"-> {store.get(room_id) or 'default'}")

    # -- /btw command -----------------------------------------------------------

    def _btw_room(self, room_id: str, text: str, ev: dict) -> None:
        """``/btw <text>``: interject into the action(s) running in the room.

        Each in-flight run's newest OpenCode session gets the text as a
        ``steer`` prompt — the running turn sees it without a new run being
        started.  The message gets the usual wrench while handling lasts.
        """
        if not text:
            self._send_note(room_id, "📣 Usage: /btw <text> — interject into "
                                     "the action running in this room.")
            return
        key = room_id.casefold()
        with self._session_lock:
            live = list(self._sessions.get(key, []))
        targets: list[tuple[_Session, str]] = []
        for sess in live:
            with self._session_lock:
                ids = list(sess.session_ids)
            if ids:
                targets.append((sess, ids[-1]))
        if not targets:
            self._send_note(room_id, "📣 Nothing is running in this room — "
                                     "/btw interjects into a running action.")
            return
        delivered = 0
        for sess, sid in targets:
            try:
                self.bot.opencode.steer(sid, text)
                delivered += 1
            except OpenCodeError as exc:
                _log(f"{self.tag} /btw steer {sid} failed: {exc}")
        if delivered:
            rid = self._react_on(room_id, ev.get("event_id"))
            if rid:
                with self._session_lock:
                    targets[0][0].reactions.append(rid)
            self._send_note(room_id, f"📣 Interjection delivered to "
                                     f"{delivered} running action(s).")
        else:
            self._send_note(room_id, "📣 Could not deliver the interjection "
                                     "(the run may have just ended).")
        _log(f"{self.tag} /btw for {room_id} -> {delivered}/{len(targets)} "
             f"session(s) steered")

    # -- per-message pipeline ---------------------------------------------------

    def _handle(self, ev: dict) -> None:
        if ev.get("sender") == self.bot.user_id:
            return  # from the bot itself: never waits, never triggers
        room_id = ev["room_id"]
        room_name = self._name_for(room_id, ev.get("room_name"))
        where = f"room={room_name or room_id}"

        if is_stop_command(ev.get("body")):
            self._stop_room(room_id, where)
            return

        is_effort, level = parse_effort_command(ev.get("body"))
        if is_effort:
            self._effort_room(room_id, level)
            return

        is_btw, btw_text = parse_btw_command(ev.get("body"))
        if is_btw:
            self._btw_room(room_id, btw_text, ev)
            return

        if self.waiters.deliver(self.bot.name, room_id, ev):
            # Delivered to a model that is waiting for a reply.  If there is a
            # run in flight for this room, mark this message as being handled
            # too (its wrench is removed when that run finishes / is stopped).
            key = room_id.casefold()
            with self._session_lock:
                live = self._sessions.get(key)
                target = live[0] if live else None
            if target is not None:
                rid = self._react_on(room_id, ev.get("event_id"))
                if rid:
                    with self._session_lock:
                        target.reactions.append(rid)
            _log(f"{self.tag} {where} sender={ev.get('sender')} "
                 f"-> delivered to waiting client (triggers suppressed)")
            return

        for trig in self.bot.cfg.triggers:
            if not trig.matches(room_id, room_name):
                continue
            _log(f"{self.tag} {where} sender={ev.get('sender')} "
                 f"-> running trigger {trig.room!r}")
            threading.Thread(target=self._fire, args=(trig, ev),
                             name=f"trigger-{self.bot.name}-{trig.room[:12]}".replace("/", "_"),
                             daemon=True).start()

    # -- trigger execution ------------------------------------------------------

    def _fire(self, trig, ev: dict) -> None:
        tag = f"[trigger:{self.bot.name}:{trig.room}]"
        room_id = ev["room_id"]
        key = room_id.casefold()

        sess = _Session(room_id)
        with self._session_lock:
            self._sessions.setdefault(key, []).append(sess)

        # Mark the triggering message as being handled.
        rid = self._react_on(room_id, ev.get("event_id"))
        if rid:
            with self._session_lock:
                sess.reactions.append(rid)

        try:
            self._run_action(tag, trig, sess, room_id, ev)
        finally:
            with self._session_lock:
                live = self._sessions.get(key)
                if live and sess in live:
                    live.remove(sess)
            self._clean_up(sess)
            _log(f"{tag} run finished for {room_id}")

    def _run_action(self, tag: str, trig, sess: _Session,
                    room_id: str, ev: dict) -> None:
        room_name = ev.get("room_name")
        try:
            history = self.bot.client.read_messages(room_id, limit=trig.context,
                                                    crypto=self.bot.crypto)
        except MatrixError as exc:
            _log(f"{tag} reading history failed: {exc}")
            history = [ev]
        if not any(m.get("event_id") == ev.get("event_id") for m in history):
            # Sync was first: make sure the triggering message is in context.
            history = [*history, ev]
            history = history[-trig.context:]
        values = {
            "channel": room_name or room_id,
            "room": room_id,
            "sender": ev.get("sender") or "",
            "msg": format_history(history),
        }
        action = trig.action
        if sess.cancelled:
            _log(f"{tag} cancelled before the action started")
            return
        started = time.monotonic()

        def _note_session(sid: str) -> None:
            with self._session_lock:
                sess.session_ids.append(sid)

        result = self.bot.opencode.run(
            title=f"MatrixWebhooker {self.bot.name} {room_name or room_id}",
            prompt=render_text(action.prompt, values),
            system=action.system, model=action.model, agent=action.agent,
            directory=action.directory or str(BASE_DIR),
            variant=self.bot.effort.get(room_id),
            timeout=action.timeout, cancel=lambda: sess.cancelled,
            keep_session=action.keep_session, on_session=_note_session,
            log=lambda m: _log(f"{tag} {m}"))
        dt = time.monotonic() - started
        if result.outcome == "succeeded":
            _log(f"{tag} answered in {dt:.1f}s ({len(result.text)} chars)")
            if result.text and not sess.cancelled:
                try:
                    self.bot.client.send_message(
                        room_id, {"msgtype": "m.text", "body": result.text})
                except MatrixError as exc:
                    _log(f"{tag} posting the answer failed: {exc}")
            elif not result.text:
                _log(f"{tag} the model produced no text — nothing to post")
        elif result.outcome == "timeout":
            _log(f"{tag} no answer after {dt:.1f}s (action.timeout)")
        elif result.outcome == "interrupted":
            _log(f"{tag} interrupted after {dt:.1f}s")
        else:
            _log(f"{tag} action failed after {dt:.1f}s: {result.error} "
                 f"(session {result.session_id} kept for inspection)")
        if sess.cancelled:
            _log(f"{tag} stopped by user command")
