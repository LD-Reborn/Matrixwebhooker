#!/usr/bin/env python3
"""Incoming-message watcher, per-bot triggers and reply waiting.

Runs inside ``server.py`` (the long-lived process): one ``MessageWatcher``
thread per bot long-polls Matrix ``/sync`` and, for every room message the
bot receives — any channel it is in, including DMs — in this order:

  1. messages sent by the bot itself are ignored (never a trigger);
  2. a bare "stop" keyword (``stop`` / ``/stop`` / ``STOP`` / ``STOP!!!`` —
     starts with "stop" and contains no whitespace) halts the current
     execution for that room: the running trigger command is killed, any
     pending waiters (a model blocked in ``wait_for_reply``) are released,
     and every wrench reaction added while it was handled is removed again.
     The stop command is never delivered to a waiter or fired as a trigger.
     The kill is a signal to the command's *process group*, so the work must
     actually run inside that process tree: e.g. ``opencode run`` needs
     ``--standalone`` — without it it is only a client of the shared OpenCode
     background service, and killing the client would leave that service
     running the turn (and posting the answer);
  3. if a client is waiting for a reply in that room (``POST /wait``, or
     the MCP ``wait_for_reply`` tool), the message is handed to the oldest
     waiter instead of running any triggers.  While a trigger run for that
     room is still in flight, the delivered message is additionally marked
     with the wrench reaction (see below);
  4. otherwise every trigger of the bot whose room matches is fired in a
     background thread: the bash command line(s) with ``{{channel}}``,
     ``{{msg}}`` (last ``context`` messages, newest last), ``{{sender}}``,
     ``{{room}}``, ``{{message_file}}`` and ``{{history_file}}``
     placeholders (values are shell-quoted), run in the app directory with
     ``bash`` (falls back to ``sh`` if no bash, e.g. in the slim Docker
     image).  The file placeholders point at JSON files (fresh per run,
     removed after the command(s) exit) so dynamic content can be handed to
     a program without any shell-quoting issues.

Wrench feedback (🔧):
    Whenever a message is *handled* — a trigger for its room is fired, or it
    is delivered to a model that is already waiting for a reply in that room
    while that trigger run is still in flight — the bot reacts to it with the
    wrench emoji ``🔧``.  As soon as handling of that message is finished
    (the trigger command(s) exit, or the room's "stop" command is received),
    the reaction is removed again (the reaction event is redacted), so you
    can tell at a glance which messages the bot is still working on.

``mcp_server.py`` deliberately runs no watcher of its own: its
``wait_for_reply`` tool talks to this server, so waits and trigger
suppression always live in exactly one process (no double-firing).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque

from matrixbot import BASE_DIR, Bot, MatrixError

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


#: Per-run JSON context lives here (one sub-directory per fired trigger).
CONTEXT_DIR = BASE_DIR / ".trigger-context"

#: Context directories older than this are removed on the next trigger fire
#: (covers runs that were killed and never got a chance to clean up).
CONTEXT_TTL_SECONDS = 24 * 3600


def _sweep_stale_context() -> None:
    try:
        for entry in CONTEXT_DIR.iterdir():
            try:
                if time.time() - entry.stat().st_mtime > CONTEXT_TTL_SECONDS:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                pass
    except OSError:  # directory missing: nothing to sweep
        pass


def write_context_files(room_id: str, room_name: str | None, ev: dict,
                        history: list[dict]) -> tuple[str, str]:
    """Write this message and the recent room history as JSON into a fresh
    per-run directory and return ``(message_file, history_file)``.

    ``message.json``  — the triggering message (raw body).
    ``history.json``  — the last ``context`` messages, oldest first, newest
                        last (the same content ``{{msg}}`` renders, structured).

    Files are 0600, the directory 0700 (/tmp-backed tools aside, this may sit
    inside the app directory, see CONTEXT_DIR).
    """
    _sweep_stale_context()
    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    run_dir = tempfile.mkdtemp(prefix="trig-", dir=str(CONTEXT_DIR))
    common = {"room_id": room_id, "channel": room_name or room_id}
    payloads = {
        "message.json": {**common,
                         "sender": ev.get("sender"),
                         "ts": ev.get("ts"),
                         "body": ev.get("body")},
        "history.json": {**common,
                         "oldest_first": True,
                         "messages": [{"ts": m.get("ts"),
                                       "sender": m.get("sender"),
                                       "body": m.get("body")}
                                      for m in history]},
    }
    for name, payload in payloads.items():
        path = os.path.join(run_dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.chmod(path, 0o600)
    return (os.path.join(run_dir, "message.json"),
            os.path.join(run_dir, "history.json"))


def render(template: str, values: dict[str, str]) -> str:
    """Replace {{placeholder}} with shell-quoted values (safe to embed)."""
    out = template
    for key, value in values.items():
        out = out.replace("{{%s}}" % key, shlex.quote(value))
    return out


def _shell_prefix() -> list[str]:
    return ["bash", "-c"] if shutil.which("bash") else ["sh", "-c"]


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    """Signal the whole process group of ``proc`` (it was started with its own
    session), so we also reach the children of ``bash -c`` (e.g. ``opencode``)."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError) as exc:
        _log(f"  killpg(pid={proc.pid}, sig={sig!r}) failed: {exc}")


# ---------------------------------------------------------------------------
# Per-bot watcher
# ---------------------------------------------------------------------------


class _Session:
    """One in-flight run of a bot's triggers in a single room: the command
    process(es) currently running, the wrench-reaction events added on the
    room's behalf while it runs, and its state.  Wrench removal happens
    exactly once (whichever of the run's end / a stop claim wins it via
    ``cleaned``)."""

    __slots__ = ("room_id", "procs", "reactions", "cancelled", "cleaned")

    def __init__(self, room_id: str) -> None:
        self.room_id = room_id
        self.procs: list[subprocess.Popen] = []
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

    def _kill_running(self, sess: _Session) -> None:
        """Terminate the session's still-running command(s): SIGTERM the whole
        process group, wait briefly, then SIGKILL survivors."""
        with self._session_lock:
            procs = [p for p in sess.procs if p.poll() is None]
        if not procs:
            return
        for p in procs:
            _signal_group(p, signal.SIGTERM)
        deadline = time.monotonic() + 3.0
        for p in procs:
            try:
                p.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                _signal_group(p, signal.SIGKILL)
                try:
                    p.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    _log(f"{self.tag} pid {p.pid} did not die")

    def _ack_stop(self, room_id: str, message: str) -> None:
        try:
            self.bot.client.send_message(room_id,
                                         {"msgtype": "m.text", "body": message})
        except MatrixError as exc:
            _log(f"{self.tag} sending stop acknowledgment failed: {exc}")

    def _stop_room(self, room_id: str, where: str) -> None:
        """Handle a bare "stop" in ``room_id``: kill the run, release waiters
        and clean up the wrench reactions."""
        key = room_id.casefold()
        with self._session_lock:
            active = list(self._sessions.get(key, []))
        for sess in active:
            sess.cancelled = True
            self._kill_running(sess)
            self._clean_up(sess)
        cancelled = self.waiters.cancel(self.bot.name, room_id)
        if active:
            ack = "⏹ Stopped."
        elif cancelled:
            ack = "⏹ Stop sent (waiting model released)."
        else:
            ack = "⏹ Nothing was running."
        self._ack_stop(room_id, ack)
        _log(f"{self.tag} {where} -> stopped: {len(active)} run(s), "
             f"{cancelled} waiter(s) released")

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
            self._run_commands(tag, trig, sess, room_id, ev)
        finally:
            with self._session_lock:
                live = self._sessions.get(key)
                if live and sess in live:
                    live.remove(sess)
            self._clean_up(sess)
            _log(f"{tag} run finished for {room_id}")

    def _run_commands(self, tag: str, trig, sess: _Session,
                      room_id: str, ev: dict) -> None:
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
        # JSON files for the command(s) to read ({{message_file}} /
        # {{history_file}}) — dynamic content stays out of the shell line.
        values["message_file"], values["history_file"] = write_context_files(
            room_id, room_name, ev, history)
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
            if sess.cancelled:
                _log(f"{tag} cancelled before command: {command[:80]}")
                break
            cmdline = render(command, values)
            _log(f"{tag} run: {cmdline[:300]}")
            started = time.monotonic()
            try:
                proc = subprocess.Popen(prefix + [cmdline], env=env, cwd=str(BASE_DIR),
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, start_new_session=True)
            except Exception as exc:  # noqa: BLE001
                _log(f"{tag} failed to start: {exc!r}")
                continue
            with self._session_lock:
                sess.procs.append(proc)
            try:
                out, err = proc.communicate()
            except Exception as exc:  # noqa: BLE001
                out, err = "", ""
                _log(f"{tag} error while reaping command: {exc!r}")
            finally:
                with self._session_lock:
                    try:
                        sess.procs.remove(proc)
                    except ValueError:
                        pass
            dt = time.monotonic() - started
            if proc.returncode == 0:
                _log(f"{tag} done (exit 0, {dt:.1f}s)")
            else:
                tail = ((str(err or out) or "") + "\n").strip()[-500:]
                _log(f"{tag} exit {proc.returncode} after {dt:.1f}s: {tail}")
        if sess.cancelled:
            _log(f"{tag} stopped by user command")
        # The command(s) are done; their context files are no longer needed.
        shutil.rmtree(os.path.dirname(values["message_file"]), ignore_errors=True)
