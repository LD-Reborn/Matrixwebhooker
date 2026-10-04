#!/usr/bin/env python3
"""Incoming-message watcher, per-bot triggers and reply waiting.

Runs inside ``server.py`` (the long-lived process): one ``MessageWatcher``
thread per bot long-polls Matrix ``/sync`` and, for every room message the
bot receives — any channel it is in, including DMs — in this order:

  1. messages sent by the bot itself are ignored (never a trigger);
  2. if a client is waiting for a reply in that room (``POST /wait``, or
     the MCP ``wait_for_reply`` tool), the message is handed to the oldest
     waiter instead of running any triggers;
  3. otherwise every trigger of the bot whose room matches is fired in a
     background thread: the bash command line(s) with ``{{channel}}``,
     ``{{msg}}`` (last ``context`` messages, newest last), ``{{sender}}``
     and ``{{room}}`` placeholders (values are shell-quoted), run in the
     app directory with ``bash`` (falls back to ``sh`` if no bash, e.g. in
     the slim Docker image).

``mcp_server.py`` deliberately runs no watcher of its own: its
``wait_for_reply`` tool talks to this server, so waits and trigger
suppression always live in exactly one process (no double-firing).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections import deque

from matrixbot import BASE_DIR, Bot, MatrixError

#: Hard cap (seconds) for POST /wait and the MCP wait_for_reply tool.
MAX_WAIT_SECONDS = 600


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Waiting for a reply (consumers of incoming messages, ahead of triggers)
# ---------------------------------------------------------------------------


class _Waiter:
    __slots__ = ("event", "message")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.message: dict | None = None


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


def render(template: str, values: dict[str, str]) -> str:
    """Replace {{placeholder}} with shell-quoted values (safe to embed)."""
    out = template
    for key, value in values.items():
        out = out.replace("{{%s}}" % key, shlex.quote(value))
    return out


def _shell_prefix() -> list[str]:
    return ["bash", "-c"] if shutil.which("bash") else ["sh", "-c"]


# ---------------------------------------------------------------------------
# Per-bot watcher
# ---------------------------------------------------------------------------


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

    def start(self) -> None:
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
            _log(f"{self.tag} primed ({len(result['events'])} historical "
                 f"message(s) skipped)")
            return
        if not result["events"]:
            return
        _log(f"{self.tag} +{len(result['events'])} message(s)")
        for ev in result["events"]:
            self._handle(ev)

    # -- per-message pipeline ---------------------------------------------------

    def _handle(self, ev: dict) -> None:
        if ev.get("sender") == self.bot.user_id:
            return  # from the bot itself: never waits, never triggers
        room_id = ev["room_id"]
        room_name = self._name_for(room_id, ev.get("room_name"))
        where = f"room={room_name or room_id}"
        if self.waiters.deliver(self.bot.name, room_id, ev):
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
        room_name = ev.get("room_name")
        try:
            history = self.bot.client.read_messages(room_id, limit=trig.context)
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
        env = dict(
            os.environ,
            MATRIX_BOT=self.bot.name,
            MATRIX_ROOM=room_id,
            MATRIX_CHANNEL=room_name or room_id,
            # Convenience default for the matrix MCP tools in the spawned run.
            MCP_BOT=self.bot.name,
        )
        prefix = _shell_prefix()
        for command in trig.commands:
            cmdline = render(command, values)
            _log(f"{tag} run: {cmdline[:300]}")
            started = time.monotonic()
            try:
                proc = subprocess.run(prefix + [cmdline], env=env, cwd=str(BASE_DIR),
                                      capture_output=True, text=True)
            except Exception as exc:  # noqa: BLE001
                _log(f"{tag} failed to start: {exc!r}")
                continue
            dt = time.monotonic() - started
            if proc.returncode == 0:
                _log(f"{tag} done (exit 0, {dt:.1f}s)")
            else:
                tail = ((proc.stderr or proc.stdout or "") + "\n").strip()[-500:]
                _log(f"{tag} exit {proc.returncode} after {dt:.1f}s: {tail}")
