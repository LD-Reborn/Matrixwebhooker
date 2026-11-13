#!/usr/bin/env python3
"""Incoming-message watcher, per-bot triggers and reply waiting.

Runs inside ``server.py`` (the long-lived process): one ``MessageWatcher``
thread per bot long-polls Matrix ``/sync`` and, for every room message the
bot receives — any channel it is in, including DMs — in this order:

  1. messages sent by the bot itself are ignored (never a trigger);
  1b. invisible-to-bots marker: a message whose body *starts* with a
      zero-width character (U+200C ZWNJ or U+FEFF BOM — both verified to
      survive the Matrix wire untouched and to render invisibly) is
      completely ignored: no trigger, no command, no waiter, no reaction.
      Every meta text the bot posts itself (``/help``, ``/status``, acks,
      failure notices — everything via ``_send_note``) carries the U+200C
      prefix, so peer bots never echo each other, independent of whether
      ``peer_bots`` is configured on every deployment.  Real model answers
      are never marked.
  2. a bare "stop" keyword (``stop`` / ``/stop`` / ``STOP`` / ``STOP!!!`` —
     starts with "stop" and contains no whitespace) halts the current
     execution for that room: every OpenCode session the running trigger
     started is interrupted (``opencode_api``), any pending waiters (a model
     blocked in ``wait_for_reply``) are released, and every wrench reaction
     added while it was handled is removed again.  The stop command is never
     delivered to a waiter or fired as a trigger.  It is acked by *reacting*
     to the stop message (🛑 something stopped / ⭕ nothing was running) —
     a text ack would re-trigger other bots in multi-bot rooms (echo loops).
  2a. ``/help`` posts the command menu; ``/status`` reports the live
      activity of every run in flight (thinking / running a tool /
      writing the answer — see ``opencode_api.session_activity``) plus
      the code version (``version.app_version``, git describe) and the
      room's session, effort and mute state.  Both always work, also
      when muted.
  2b. ``/mute [@bot] [minutes]`` mutes this bot in this room (a
      ``@name`` targeting another user only makes the bot it matches
      act, the others stay silent; no minutes = permanent mute),
      ``/mute 0`` or ``/unmute [@bot]`` ends it.  A muted bot ignores
      everything from here on (triggers, waiters, ``/effort``, ``/btw``,
      ``/new``, ``/revert``) except ``stop``, ``/help``, ``/status``,
      ``/mute`` and ``/unmute``.  Mutes are persisted in the bot's
      state dir.
  2c. ``/effort`` (list the reasoning-effort levels of the room) and
      ``/effort <default|low|mid|high|xhigh>`` (set it for the room, persisted
      in the bot's state dir) — the room's level becomes the model
      ``variant`` of every action run started in that room afterwards.
  2d. ``/btw <text>`` interjects into the action(s) currently running for
      that room (OpenCode inbox ``steer`` delivery) instead of starting a
      new run or feeding a waiter.
  2e. ``/new [text]`` and a bare ``/end`` close the room's live OpenCode
      session (see 4): ``/end`` and ``/new`` without text only ack (the
      next message starts fresh), ``/new <text>`` immediately runs the
      text as a prompt in a fresh session.
  2f. ``/revert [text]`` re-fires the room's last trigger message — always
      in a fresh session (the live one is closed first); with text, the
      trigger runs on that updated message instead of the original one.
  2g. mention gate: a message that *tags* someone explicitly (an
      ``m.mentions`` pill, or a plain ``@user:server`` in the text) is
      meant for that person — the bot stays completely silent unless it is
      tagged **too** (alone or among others; ``@room``/``@here`` tag it as
      well). Messages that tag nobody are for everyone and handled as
      before. This keeps one bot from answering messages another bot (or
      human) was addressed with.  Commands are addressable the same way:
      a message that *starts* with mentions is parsed as a command behind
      them (``@bot /status``, ``@bot stop``) and only runs on the tagged
      bots — the others stay silent instead of answering it as chat.
  2h. sibling-bot gate: messages from the roster's *other* bots (the bot
      users in ``bots.json``, plus foreign webhookers' bots listed in the
      top-level ``peer_bots``) only fire triggers when this bot is tagged
      explicitly — untagged bot chatter is ignored, so two agents cannot
      answer each other forever. Humans are unaffected. In addition, a
      run whose final text is exactly ``NO_REPLY`` posts nothing at all:
      that is the escape hatch a prompt should offer the model for
      "nothing to add".
  3. if a client is waiting for a reply in that room (``POST /wait``, or
     the MCP ``wait_for_reply`` tool), the message is handed to the oldest
     waiter instead of running any triggers.  While a trigger run for that
     room is still in flight, the delivered message is additionally marked
     with the wrench reaction (see below);
  4. otherwise every trigger of the bot whose room matches is fired in a
     background thread: its ``action`` runs on the OpenCode server
     (``opencode_api.py``) with the ``{{channel}}``, ``{{msg}}`` (last
     ``context`` messages, newest last), ``{{sender}}`` and ``{{room}}``
     placeholders substituted into the prompt — and the final assistant
     text is posted back into the room.
     *Room sessions*: a succeeded run's session stays open as the room's
     live conversation; the next message of that room within
     ``SESSION_WINDOW_SECONDS`` (120 min, re-armed by every run) is
     admitted to that session as a continuation prompt (``{{msg}}`` then
     carries only the new message — the session already knows the rest),
     and only after the window closes (or after ``/new``/``/end``/
     ``/revert``) does the next message start a fresh session.  An unused,
     expired session is deleted by a sweep on every sync round.

Wrench feedback (🔧):
    Whenever a message is *handled* — a trigger for its room is fired, or it
    is delivered to a model that is already waiting for a reply in that room
    while that trigger run is still in flight — the bot reacts to it with the
    wrench emoji ``🔧``.  As soon as handling of that message is finished
    (the trigger action ends, or the room's "stop" command is received),
    the reaction is removed again (the reaction event is redacted), so you
    can tell at a glance which messages the bot is still working on.

Failure feedback:
    A run that ends without an answer never goes silent: unless a stop
    command cancelled it, the room gets a short notice — ``⚠️`` with the
    server's error (and the kept OpenCode session id) when the action
    failed, ``⏳`` on ``action.timeout``, ``🤔`` when the model finished
    without producing any text.  The full details always go to the log.

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
from version import app_version

#: Hard cap (seconds) for POST /wait and the MCP wait_for_reply tool.
MAX_WAIT_SECONDS = 600

#: The reaction the bot adds to a message while it is being handled, and
#: removes again as soon as handling of that message is over.
WRENCH_REACTION = "🔧"
#: reactions acking a bare "stop" — never a text message: in multi-bot
#: rooms an ack text re-triggers the other bots (endless echo loops)
STOP_REACTION = "🛑"
NOTHING_TO_STOP_REACTION = "⭕"
#: the token a model replies with when it has nothing to say — the run
#: then posts NOTHING into the room (any bot text would re-trigger the
#: other bots listening in that room)
SILENT_REPLY = "NO_REPLY"

#: When the watcher sees a room for the first time mid-run, the bot has just
#: joined it and /sync delivers part of that room's recent history along
#: with the actually new messages.  Events older than this grace window in
#: such a room are backlog: acting on them would fire triggers (or stop
#: commands) on week-old text.  Fresher ones are treated as real messages,
#: so "invite the bot, say hi" still works immediately.
BACKLOG_GRACE_SECONDS = 120


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


#: a full Matrix user id in plain text — typed by hand or the reply-fallback
#: quote clients put in front of a reply
_MXID_RE = re.compile(r"@[A-Za-z0-9._=/+-]+:[A-Za-z0-9.-]+")
#: quoted reply-fallback lines (``> <@user:server> …``): they quote someone
#: else's message instead of tagging them — not a tag by this sender
_QUOTED_RE = re.compile(r"^\s*>.*$", re.MULTILINE)


def extract_mentions(ev: dict) -> set[str]:
    """User ids (lower-cased) that a message event explicitly tags.

    The canonical source is the ``m.mentions`` property (kept as
    ``ev["mentions"]`` by ``matrixbot.sync()``) that clients set for pills;
    full ``@user:server`` strings in the plain body count too, since not
    every client sends ``m.mentions``. Quoted reply-fallback lines are
    ignored: they belong to the quoted message, not to this sender.
    """
    ids: set[str] = set()
    prop = ev.get("mentions")
    if isinstance(prop, dict):
        for uid in prop.get("user_ids") or []:
            if isinstance(uid, str):
                ids.add(uid.casefold())
    body = ev.get("body") or ""
    ids.update(x.casefold() for x in _MXID_RE.findall(_QUOTED_RE.sub("", body)))
    return ids


def tags_bot_itself(ev: dict, user_id: str) -> bool:
    """True when the message tags this bot explicitly: pill or plain
    ``@user:server`` text, or an ``@room``/``@here`` broadcast (tags all).
    """
    prop = ev.get("mentions")
    if isinstance(prop, dict) and prop.get("room"):
        return True
    return user_id.casefold() in extract_mentions(ev)


def addressed_to_bot(ev: dict, user_id: str) -> bool:
    """Whether the bot may act on this message at all (mention gate).

    A message that tags nobody is addressed to the whole room and the bot
    reacts as configured. But once the sender tags someone explicitly, the
    message is clearly meant for that person: the bot only reacts when it
    is tagged *too* (alone or among others). ``@room``/``@here`` broadcasts
    tag the bot as well and therefore always count.
    """
    return not extract_mentions(ev) or tags_bot_itself(ev, user_id)


def is_silent_reply(text: str | None) -> bool:
    """True when the final assistant text is exactly the ``NO_REPLY``
    token (punctuation and case tolerated) — the model's way of saying
    "nothing to add", and the run then posts nothing at all.
    """
    if not text:
        return False
    return text.strip().strip(".!?,;:…\"'«»()[]").casefold() == SILENT_REPLY.casefold()


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


_NEW_RE = re.compile(r"^/new\b[\s,]*(.*)$", re.IGNORECASE | re.DOTALL)
_REVERT_RE = re.compile(r"^/(?:revert|retry)\b[\s,]*(.*)$",
                        re.IGNORECASE | re.DOTALL)
_MUTE_RE = re.compile(r"^/mute\b[\s,]*(.*)$", re.IGNORECASE | re.DOTALL)
_UNMUTE_RE = re.compile(r"^/unmute\b[\s,]*(.*)$", re.IGNORECASE | re.DOTALL)


#: ``@user[:domain]`` / ``@room`` / ``@here`` tokens at the very start of a
#: message — they may introduce a command addressed to one bot
#: ("@kraken /status"), in which case only the tagged bots act
_CMD_MENTION_RE = re.compile(
    r"^(@[A-Za-z0-9._=/+-]+(?::[A-Za-z0-9.-]+)?)[\s,]+")


def strip_leading_mentions(body: str | None) -> tuple[str | None, list[str]]:
    """Peel leading ``@user[:domain]`` tokens (plus whitespace) off a body:
    ``(remaining, mentioned)``.

    Lets ``@bot /status`` or ``@bot stop`` address a command to a single
    bot in a group chat: the command then parses against the remainder and
    only the tagged bots act — the others stay silent.  Messages without a
    leading mention come back untouched (``mentioned`` empty).
    """
    if not body:
        return body, []
    text = body
    mentioned: list[str] = []
    while True:
        m = _CMD_MENTION_RE.match(text)
        if m is None:
            return text, mentioned
        mentioned.append(m.group(1))
        text = text[m.end():]


def _mention_token_is_me(token: str, names: set[str]) -> bool:
    """True when a leading ``@name`` / ``@user:domain`` token names this
    bot (local part compared, domain ignored) or broadcasts (``@room``,
    ``@here``)."""
    t = token.casefold().lstrip("@")
    return t in ("room", "here") or t.split(":", 1)[0] in names


#: command names ``_looks_like_command`` recognizes (keep in sync with the
#: parsers the pipeline runs before the mention gates)
_COMMAND_NAMES = {"help", "status", "mute", "unmute", "effort", "btw",
                  "new", "end", "revert", "retry"}


def _looks_like_command(body: str | None) -> bool:
    """True when the body is one of the bot commands — used to recognize a
    command addressed to *another* bot and stay silent instead of answering
    it as ordinary chat."""
    if not body or not body.strip():
        return False
    text = body.strip()
    if is_stop_command(text):
        return True
    if not text.startswith("/"):
        return False
    head = text.partition(" ")[0].casefold().lstrip("/").rstrip("!?.…:;")
    return head in _COMMAND_NAMES


def _parse_bare(body: str | None, name: str) -> bool:
    """True when the whole message is ``/<name>`` — leading ``/`` required,
    case ignored, trailing punctuation like ``STOP!!!`` tolerated (the stop
    command's rules), no arguments.
    """
    if not body:
        return False
    text = body.strip()
    if not text.startswith("/"):
        return False
    return text.casefold().lstrip("/").rstrip("!?.…:;") == name


def parse_help_command(body: str | None) -> bool:
    """True for a bare ``/help`` — and nothing else.

    The whole message must be the command (leading ``/`` required, case
    and trailing punctuation ignored).
    """
    return _parse_bare(body, "help")


def parse_end_command(body: str | None) -> bool:
    """True for a bare ``/end`` (closes the room's live session)."""
    return _parse_bare(body, "end")


def parse_revert_command(body: str | None) -> tuple[bool, str]:
    """True for ``/revert`` or ``/revert <text>`` — re-run the room's last
    trigger message in a fresh session; the text (if any) replaces the
    original message.  ``/retry`` is accepted as a hidden alias.
    """
    if not body:
        return False, ""
    m = _REVERT_RE.match(body.strip())
    if m is None:
        return False, ""
    return True, m.group(1).strip()


def parse_status_command(body: str | None) -> bool:
    """True for a bare ``/status`` (activity + session report)."""
    return _parse_bare(body, "status")


def parse_unmute_command(body: str | None) -> tuple[bool, str]:
    """True for ``/unmute`` or ``/unmute [@bot]``."""
    if not body:
        return False, ""
    m = _UNMUTE_RE.match(body.strip())
    if m is None:
        return False, ""
    return True, m.group(1).strip()


def parse_mute_command(body: str | None) -> tuple[bool, str]:
    """True for ``/mute`` with any argument text (``[@bot] [minutes]``).

    ``/unmute`` never matches: its body starts with ``/u``, not ``/m``.
    Argument validation (bot match, minutes range) happens in the handler.
    """
    if not body:
        return False, ""
    m = _MUTE_RE.match(body.strip())
    if m is None:
        return False, ""
    return True, m.group(1).strip()


def parse_new_command(body: str | None) -> tuple[bool, str]:
    """True for ``/new`` or ``/new <text>`` — close the room's live session.

    With text, a fresh session runs that prompt immediately; without, the
    handler only acks and the next message starts fresh.
    """
    if not body:
        return False, ""
    m = _NEW_RE.match(body.strip())
    if m is None:
        return False, ""
    return True, m.group(1).strip()


#: leading zero-width characters that make a message invisible to bots
#: (both verified to survive the Matrix wire untouched and to render as
#: nothing — clients show no tofu)
IGNORE_MARKERS = ("\u200c", "\ufeff")

#: the marker this bot prefixes its own meta texts with (``_send_note``),
#: so peer bots never treat an ack or notice as something to answer —
#: works even where the ``peer_bots`` roster is not configured
META_MARK = "\u200c"

#: how long a room's live OpenCode session stays open after a finished run:
#: the next message of the room within this window continues the session
#: (every run re-arms the window), afterwards — or after ``/new``, ``/end``,
#: ``/revert`` — the next message starts a fresh one
SESSION_WINDOW_SECONDS = 120 * 60

#: timeout for command-started runs that have no action behind them
#: (``/new <text>`` in a room without triggers); trigger-backed runs use
#: their own ``action.timeout``
RUN_TIMEOUT_DEFAULT = 300

#: upper bound accepted for ``/mute`` minutes (24 h)
MUTE_MAX_MINUTES = 1440

#: the menu ``/help`` posts into the room
HELP_TEXT = (
    "ℹ️ Commands in this room:\n"
    "• stop — interrupt the action running here (acked by reaction)\n"
    "• /help — this menu\n"
    "• /status — what the bot is doing right now + room session state\n"
    "• /effort [default|low|mid|high|xhigh] — show or set the reasoning "
    "effort for this room\n"
    "• /btw <text> — interject into the action currently running here\n"
    f"• /new [text] / /end — close this room's session (it stays open "
    f"{SESSION_WINDOW_SECONDS // 60} min between messages); /new <text> "
    "starts a fresh run right away\n"
    "• /revert [text] — re-run this room's last trigger message in a fresh "
    "session; with text, on the updated message instead\n"
    "• /mute [@bot] [minutes] — mute this bot here (no minutes = "
    "permanent); /mute 0 or /unmute unmutes\n"
    f"Any message within {SESSION_WINDOW_SECONDS // 60} min of the last "
    "answer continues this room's session; a message starting with a "
    "zero-width character is invisible to bots. In group chats you can "
    "address a command to one bot with a leading mention "
    "(@bot /status). Everything else is handled by this room's trigger."
)


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

    def __init__(self, bot: Bot, waiters: WaitRegistry,
                 peer_ids=frozenset()):
        self.bot = bot
        self.waiters = waiters
        #: user ids of the roster's *other* bots (from bots.json): their
        #: messages only fire triggers when this bot is tagged explicitly,
        #: otherwise two agents answer each other forever
        self._peer_bots = frozenset(x.casefold() for x in peer_ids)
        self._since: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # room_id -> display name. Incremental /sync responses carry no
        # unchanged state, so names learned from the initial snapshot (or
        # the state API) are kept here for trigger matching.
        self._room_names: dict[str, str] = {}
        # Rooms this watcher has already seen events from.  A room that shows
        # up for the first time mid-session was joined after startup: /sync
        # then delivers its history as if it were news, so clearly-old events
        # in it are backlog and must not fire triggers or stop commands.
        self._known_rooms: set[str] = set()
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
        #: The room's live OpenCode session (casefolded room_id -> {"id":
        #: session id, "expires": epoch, "managed": whether the webhooker
        #: may delete it — false for ``keep_session`` actions).  A message
        #: within the window continues it, afterwards it is deleted.
        #: Persisted so a restart neither forgets nor double-deletes.
        self._room_sessions: dict[str, dict] = {}
        #: last fired trigger per room for ``/revert`` (casefolded room_id ->
        #: {"trigger": <Trigger.room name>, "ev": <compact event>})
        self._fired: dict[str, dict] = {}
        self._room_session_file = (
            Path(self.bot.client.state_dir) / "room_sessions.json")

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

    def _load_room_state(self) -> None:
        """Room sessions + last-fired triggers (persisted together).

        Accepts the old ``last_session.json`` (a bare sessions map) from the
        ``/continue`` era once, then continues in the new file.
        """
        raw = None
        for path in (self._room_session_file,
                     Path(self.bot.client.state_dir) / "last_session.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                break
            except (OSError, ValueError):
                continue
        if not isinstance(raw, dict):
            return
        if "sessions" in raw or "fired" in raw:
            sessions = raw.get("sessions") or {}
            fired = raw.get("fired") or {}
        else:                                   # legacy: bare sessions map
            sessions, fired = raw, {}
        self._room_sessions = {str(k): v for k, v in sessions.items()
                               if isinstance(v, dict) and v.get("id")}
        self._fired = {str(k): v for k, v in fired.items()
                       if isinstance(v, dict) and v.get("ev")}

    def _save_room_state(self) -> None:
        """Persist sessions + fired map (safe outside the session lock —
        it takes its own snapshot)."""
        with self._session_lock:
            snapshot = {"sessions": dict(self._room_sessions),
                        "fired": dict(self._fired)}
        try:
            self._room_session_file.write_text(
                json.dumps(snapshot, indent=1, sort_keys=True),
                encoding="utf-8")
        except OSError as exc:
            _log(f"{self.tag} saving room state failed: {exc}")

    def _remember_session(self, room_id: str, session_id: str,
                          managed: bool) -> None:
        """Make this run's session the room's live conversation.

        The window re-arms on every run; the room's previous live session
        is deleted when a newer one replaces it (or later by the expiry
        sweep).  Sessions of ``keep_session`` actions are registered (so
        they stay the room's session) but never deleted by the webhooker.
        """
        key = room_id.casefold()
        with self._session_lock:
            old = self._room_sessions.get(key)
            self._room_sessions[key] = {
                "id": session_id,
                "expires": time.time() + SESSION_WINDOW_SECONDS,
                "managed": managed,
            }
        self._save_room_state()
        if old and old.get("id") != session_id and old.get("managed"):
            _log(f"{self.tag} room session {old['id']} replaced by "
                 f"{session_id} for {room_id} — deleting")
            self.bot.opencode.delete_session(old["id"])

    def _claim_session(self, room_id: str) -> dict | None:
        """Take the room's live session for the next run (``None`` when
        there is none or its window closed — expired managed sessions are
        deleted on the way)."""
        key = room_id.casefold()
        with self._session_lock:
            entry = self._room_sessions.pop(key, None)
        self._save_room_state()
        if entry and entry.get("expires", 0) > time.time():
            return entry
        if entry and entry.get("managed"):
            self.bot.opencode.delete_session(str(entry.get("id")))
        return None

    def _close_session(self, room_id: str) -> dict | None:
        """Drop the room's live session for good (``/new``, ``/end``,
        ``/revert``): managed sessions are deleted, ``keep_session`` ones
        are only un-remembered (the webhooker never deletes them)."""
        entry = self._claim_session(room_id)
        if entry and entry.get("managed"):
            self.bot.opencode.delete_session(str(entry.get("id")))
        return entry

    def _sweep_room_sessions(self) -> None:
        """Delete the live sessions whose window expired (runs on every
        sync round; deletes are best-effort)."""
        now = time.time()
        expired: list[str] = []
        changed = False
        with self._session_lock:
            for key, entry in list(self._room_sessions.items()):
                if entry.get("expires", 0) <= now:
                    del self._room_sessions[key]
                    changed = True
                    if entry.get("managed"):
                        expired.append(str(entry.get("id")))
        if not changed:
            return
        self._save_room_state()
        for sid in expired:
            _log(f"{self.tag} session window of {sid} expired — deleting")
            self.bot.opencode.delete_session(sid)

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
                    "mentions": content.get("m.mentions"),
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
        self._load_room_state()
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
        self._sweep_room_sessions()
        was_first = self._since is None
        result = self.bot.client.sync(since=self._since,
                                      timeout_ms=self.SYNC_TIMEOUT_MS)
        self._since = result.get("next_batch") or self._since
        if was_first:
            # Snapshot: learn room names, but never act on history.
            for ev in result["events"]:
                if ev.get("room_id"):
                    self._known_rooms.add(ev["room_id"])
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
        events = self._drop_backlog(result.get("events") or [])
        # The key failed events were waiting for may have just arrived —
        # retry them before processing the fresh timeline events.
        events = self._retry_pending_keys() + self._decrypt_events(events)
        if not events:
            return
        _log(f"{self.tag} +{len(events)} message(s)")
        for ev in events:
            self._handle(ev)

    def _drop_backlog(self, events: list[dict]) -> list[dict]:
        """Drop the history /sync hands over when the bot joined a room
        mid-session (that room's first non-snapshot events).

        Events older than ``BACKLOG_GRACE_SECONDS`` in a room this watcher
        has not seen yet are backlog and would fire triggers (or "stop") on
        long-answered text; each room is judged exactly once, on the batch
        it first appears in.  The check runs on the raw timeline events, so
        undecryptable backlog events are dropped before they are queued for
        a key retry.
        """
        fresh: list[dict] = []
        stale: dict[str, int] = {}
        newly_seen: set[str] = set()
        now_ms = time.time() * 1000
        for ev in events:
            rid = ev.get("room_id") or ""
            if rid in self._known_rooms:
                fresh.append(ev)
                continue
            ts = ev.get("ts") or 0
            # first time we see this room at all: judge its whole batch once
            newly_seen.add(rid)
            if ts and now_ms - ts > BACKLOG_GRACE_SECONDS * 1000:
                stale[rid] = stale.get(rid, 0) + 1
            else:
                fresh.append(ev)
        self._known_rooms |= newly_seen
        for rid, count in stale.items():
            _log(f"{self.tag} {rid}: skipped {count} backlog message(s) "
                 f"from a room joined mid-run "
                 f"(older than {BACKLOG_GRACE_SECONDS}s)")
        return fresh

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
                        "msgtype": content.get("msgtype", "m.text"),
                        "mentions": content.get("m.mentions")})
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
        """Post a short status text into the room (command acks, action
        failures — the things that must never pass silently).

        Always carries the invisible ``META_MARK`` prefix, so peer bots
        treat it as human-only chatter and never answer it (module
        docstring 1b); real model answers are posted elsewhere, unmarked.
        """
        try:
            self.bot.client.send_message(
                room_id, {"msgtype": "m.text", "body": META_MARK + message})
        except MatrixError as exc:
            _log(f"{self.tag} sending the room notice failed: {exc}")

    def _stop_room(self, room_id: str, where: str, event_id: str = "") -> None:
        """Handle a bare "stop" in ``room_id``: interrupt the run, release
        waiters, leave any call and clean up the wrench reactions.

        Acknowledged by reacting to the stop message itself — 🛑 when
        something was actually stopped, ⭕ when nothing was running. A
        text ack must not be used here: in multi-bot rooms it re-triggers
        the other bots, which then stop nothing and text back — an echo
        loop that produces nothing but noise.
        """
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
        ack = (STOP_REACTION if (active or cancelled or hung_up)
               else NOTHING_TO_STOP_REACTION)
        if event_id:
            try:
                self.bot.client.react(room_id, event_id, ack)
            except MatrixError as exc:
                _log(f"{self.tag} stop ack on {event_id} failed: {exc}")
        _log(f"{self.tag} {where} -> stopped ({ack}): {len(active)} run(s), "
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

    # -- /help command ----------------------------------------------------------

    def _help_room(self, room_id: str) -> None:
        """``/help`` posts the command menu into the room."""
        self._send_note(room_id, HELP_TEXT)
        _log(f"{self.tag} /help for {room_id}")

    # -- /status command ----------------------------------------------------------

    def _status_room(self, room_id: str) -> None:
        """``/status``: what the bot is doing in this room right now.

        For every run in flight it asks the OpenCode server for the live
        activity (``session_activity``: thinking / running a tool and
        awaiting its result / writing the answer), then adds the
        deployed code version and the room's session window, effort and
        mute state.  Works while muted.
        """
        key = room_id.casefold()
        lines: list[str] = [f"🏷️ code: {app_version()}"]
        with self._session_lock:
            active = list(self._sessions.get(key, []))
            entry = dict(self._room_sessions.get(key) or {})
        for sess in active:
            sid = sess.session_ids[-1] if sess.session_ids else ""
            state = self.bot.opencode.session_activity(sid) if sid \
                else "starting up"
            icon = ("✍️" if state.startswith("writing")
                    else {"thinking": "🧠", "idle": "💤",
                          "unknown": "❔"}.get(state, "⚙️"))
            lines.append(f"▶️ running: {icon} {state}"
                         f"{f' (session {sid})' if sid else ''}")
        left = (entry.get("expires", 0) - time.time()) / 60 \
            if entry else 0
        if left > 0:
            lines.append(f"💬 live session {entry.get('id')} — the next "
                         f"message continues it ({left:.0f} min left)")
        elif not active:
            lines.append("💬 no live session — the next message starts a "
                         "fresh one")
        lines.append(f"⚙️ effort: {self.bot.effort.get(room_id) or 'default'}")
        exp = self.bot.mute.expiry(room_id)
        if exp is not None:
            when = ("permanent — /unmute ends it" if exp == float("inf")
                    else f"until {time.strftime('%H:%M', time.localtime(exp))}")
            lines.append(f"🔇 muted in this room ({when})")
        self._send_note(room_id, "ℹ️ Status:\n" + "\n".join(lines))
        _log(f"{self.tag} /status for {room_id}")

    # -- /mute and /unmute commands --------------------------------------------------

    MUTE_USAGE = ("🔇 Usage: /mute [@bot] [minutes] — mutes this bot here "
                  "(no minutes = permanent), /mute 0 or /unmute unmutes.")

    def _targets_me(self, target: str) -> bool:
        """True when an ``@name`` / ``@user:domain`` names this bot (local
        part or configured name, domain ignored)."""
        t = target.casefold().lstrip("@").split(":", 1)[0]
        return t in {self.bot.cfg.user.casefold(), self.bot.cfg.name.casefold()}

    def _mute_room(self, room_id: str, args: str) -> None:
        """``/mute [@bot] [minutes]`` — word order of the two arguments is
        free.  With an ``@name`` only the bot it matches mutes itself, the
        others ignore the command silently (their acks would be noise).
        No minutes = permanent mute; ``/mute 0`` unmutes."""
        target = ""
        minutes = -1
        for tok in args.split():
            t = tok.strip().rstrip(":;,.!")
            if t.startswith("@"):
                if target:
                    self._send_note(room_id, self.MUTE_USAGE)
                    return
                target = t
            elif t.isdigit():
                if minutes >= 0:
                    self._send_note(room_id, self.MUTE_USAGE)
                    return
                minutes = int(t)
            else:
                self._send_note(room_id, self.MUTE_USAGE)
                return
        if minutes > MUTE_MAX_MINUTES:
            self._send_note(room_id, f"🔇 That is a lot of minutes — the "
                                     f"limit is {MUTE_MAX_MINUTES}.")
            return
        if target and not self._targets_me(target):
            _log(f"{self.tag} /mute {args!r} in {room_id} -> not me, "
                 f"ignoring")
            return
        me = self.bot.user_id
        if minutes == 0:
            self.bot.mute.set(room_id, 0)
            self._send_note(room_id, f"🔊 {me} is unmuted in this room.")
        elif minutes > 0:
            self.bot.mute.set(room_id, minutes * 60)
            until = time.strftime("%H:%M",
                                  time.localtime(time.time() + minutes * 60))
            self._send_note(room_id, f"🔇 {me} is muted in this room for "
                                     f"{minutes} min (until {until}) — "
                                     f"/unmute or /mute 0 ends it.")
        else:
            self.bot.mute.set(room_id, None)
            self._send_note(room_id, f"🔇 {me} is muted in this room until "
                                     f"/unmute.")
        _log(f"{self.tag} /mute {args or '(self, permanent)'} -> {room_id}")

    def _unmute_room(self, room_id: str, args: str) -> None:
        """``/unmute [@bot]``: end the mute (target rules as ``/mute``)."""
        tokens = args.split()
        if len(tokens) > 1 or (tokens and not tokens[0].startswith("@")):
            self._send_note(room_id, "🔊 Usage: /unmute [@bot].")
            return
        if tokens and not self._targets_me(tokens[0]):
            _log(f"{self.tag} /unmute {args!r} in {room_id} -> not me, "
                 f"ignoring")
            return
        was_muted = self.bot.mute.expiry(room_id) is not None
        self.bot.mute.set(room_id, 0)
        state = ("was muted" if was_muted else "was not muted")
        self._send_note(room_id, f"🔊 {self.bot.user_id} {state} — it is "
                                 f"unmuted in this room now.")
        _log(f"{self.tag} /unmute -> {room_id} (was_muted={was_muted})")

    # -- /new and /end commands -------------------------------------------------------

    def _end_room(self, room_id: str) -> None:
        """``/end`` (and a bare ``/new``): close the room's live session —
        the next message starts a fresh one.  Ack only, no prompt."""
        closed = self._close_session(room_id)
        self._send_note(room_id,
                        f"🆕 The room's session ({closed['id']}) is closed — "
                        f"the next message starts a fresh one."
                        if closed else
                        "🆕 This room has no live session — every message "
                        "starts a fresh one anyway.")
        _log(f"{self.tag} /end for {room_id} "
             f"closed={closed['id'] if closed else 'nothing'}")

    def _new_room(self, room_id: str, text: str, ev: dict) -> None:
        """``/new [text]``: close the live session; with text, immediately
        run it as a prompt in a fresh session (background thread)."""
        if not text:
            self._end_room(room_id)
            return
        # close the old conversation first: if the fresh run fails, the
        # room must not silently keep continuing the session /new ended
        self._close_session(room_id)
        trig = next((t for t in self.bot.cfg.triggers
                     if t.matches(room_id, ev.get("room_name"))), None)
        threading.Thread(target=self._new_run,
                         args=(room_id, text, ev, trig),
                         name=f"new-{self.bot.name}".replace("/", "_"),
                         daemon=True).start()

    def _new_run(self, room_id: str, text: str, ev: dict, trig) -> None:
        """Background run started by ``/new <text>``: always fresh session,
        with 🔧 and ``stop`` bookkeeping like a trigger run.  Uses the
        room's trigger action (persona, placeholders: ``{{msg}}`` = the
        given text); rooms without triggers get a plain prompt."""
        tag = f"[new:{self.bot.name}]"
        key = room_id.casefold()
        sess = _Session(room_id)
        with self._session_lock:
            self._sessions.setdefault(key, []).append(sess)
        rid = self._react_on(room_id, ev.get("event_id"))
        if rid:
            with self._session_lock:
                sess.reactions.append(rid)
        try:
            if trig is not None:
                action = trig.action
                values = {"channel": ev.get("room_name") or room_id,
                          "room": room_id,
                          "sender": ev.get("sender") or "",
                          "msg": text}
                prompt = render_text(action.prompt, values)
                system, model = action.system, action.model
                agent = action.agent
                directory = action.directory or str(BASE_DIR)
                timeout = action.timeout
                managed = not action.keep_session
            else:
                prompt, system, model, agent = text, "", "", ""
                directory = str(BASE_DIR)
                timeout = RUN_TIMEOUT_DEFAULT
                managed = True
            title = (f"MatrixWebhooker {self.bot.name} "
                     f"{ev.get('room_name') or room_id}")

            def _note(new_sid: str) -> None:
                with self._session_lock:
                    sess.session_ids.append(new_sid)

            result = self.bot.opencode.run(
                title=title, prompt=prompt, system=system, model=model,
                agent=agent, directory=directory,
                variant=self.bot.effort.get(room_id), timeout=timeout,
                cancel=lambda: sess.cancelled, keep_session=True,
                on_session=_note, log=lambda m: _log(f"{tag} {m}"))
            if result.outcome == "succeeded":
                _log(f"{tag} /new answered ({len(result.text)} chars)")
            else:
                _log(f"{tag} /new run outcome: {result.outcome} "
                     f"{result.error or ''}".rstrip())
            self._post_outcome(room_id, sess, result, timeout)
            if result.outcome == "succeeded" and result.session_id:
                self._remember_session(room_id, result.session_id, managed)
            elif result.session_id and managed and result.outcome != "failed":
                self.bot.opencode.delete_session(result.session_id)
        finally:
            with self._session_lock:
                live = self._sessions.get(key)
                if live and sess in live:
                    live.remove(sess)
            self._clean_up(sess)
            _log(f"{tag} /new run finished for {room_id}")

    # -- /revert command --------------------------------------------------------------

    def _revert_room(self, room_id: str, room_name: str | None,
                     text: str) -> None:
        """``/revert [text]``: revert to the room's last trigger message and
        re-fire it — always in a fresh session (the live one is closed
        first).  With text, that text *replaces* the recorded message: the
        trigger retries on the updated text while sender and room stay as
        they were."""
        key = room_id.casefold()
        with self._session_lock:
            entry = dict(self._fired.get(key) or {})
        if not entry.get("ev"):
            self._send_note(room_id, "↩️ Nothing to revert to — this room "
                                      "has no recorded trigger message yet.")
            return
        trig = next((t for t in self.bot.cfg.triggers
                     if t.room == entry.get("trigger")), None)
        if trig is None:
            trig = next((t for t in self.bot.cfg.triggers
                         if t.matches(room_id, room_name)), None)
        if trig is None:
            self._send_note(room_id, "↩️ The trigger that produced the last "
                                      "message is gone from bots.json.")
            return
        self._close_session(room_id)          # force a fresh session
        ev = dict(entry["ev"])
        ev.setdefault("room_name", room_name)
        if text:
            ev["body"] = text                 # _run_action renders the event
        _log(f"{self.tag} /revert{' (edited)' if text else ''} in {room_id}"
             f" -> re-firing {trig.room!r}")
        threading.Thread(target=self._fire, args=(trig, ev),
                         name=f"revert-{self.bot.name}".replace("/", "_"),
                         daemon=True).start()

    # -- run outcome --------------------------------------------------------------

    def _post_outcome(self, room_id: str, sess: _Session, result,
                      timeout_s: float) -> None:
        """Post a finished run's outcome into the room: the final answer
        (never ``NO_REPLY``, never after a stop) or the 🤔/⏳/⚠️ notice —
        a run that ends without an answer never passes silently."""
        if sess.cancelled:
            return
        if result.outcome == "succeeded":
            if is_silent_reply(result.text):
                _log(f"{self.tag} model replied {SILENT_REPLY} — nothing "
                     f"posted")
                return
            if result.text:
                try:
                    self.bot.client.send_message(
                        room_id, {"msgtype": "m.text", "body": result.text})
                except MatrixError as exc:
                    _log(f"{self.tag} posting the answer failed: {exc}")
            else:
                _log(f"{self.tag} the model produced no text — nothing to "
                     f"post")
                self._send_note(room_id, "🤔 The action ran to its end, "
                                         "but the model produced no text.")
        elif result.outcome == "timeout":
            self._send_note(room_id, f"⏳ The action timed out after "
                                     f"{timeout_s:.0f}s (action.timeout) — "
                                     f"no answer.")
        elif result.outcome != "interrupted":
            why = " ".join(str(result.error or "no error detail").split())
            if len(why) > 300:
                why = why[:300] + "…"
            note = f"⚠️ The action failed: {why}"
            if result.session_id:
                note += (f" (OpenCode session {result.session_id} kept "
                         "for inspection)")
            self._send_note(room_id, note)

    # -- per-message pipeline ---------------------------------------------------

    def _command_is_for_me(self, ev: dict, tokens: list[str]) -> bool:
        """True when a command in this message is addressed to this bot.

        A command that tags nobody is for the whole room (every bot acts);
        a leading ``@room``/``@here`` or a token naming this bot includes
        it; so does an ``m.mentions`` pill on it (Element turns typed
        mentions into pills plus the plain ``@user:server`` body text).
        """
        if not tokens and not extract_mentions(ev):
            return True
        if tags_bot_itself(ev, self.bot.user_id):
            return True
        names = {self.bot.cfg.user.casefold(), self.bot.cfg.name.casefold()}
        return any(_mention_token_is_me(t, names) for t in tokens)

    def _handle(self, ev: dict) -> None:
        if ev.get("sender") == self.bot.user_id:
            return  # from the bot itself: never waits, never triggers
        body = ev.get("body") or ""
        if body[:1] in IGNORE_MARKERS:
            # invisible to bots (see module docstring 1b): no trigger, no
            # command, no waiter, no reaction
            _log(f"{self.tag} {ev.get('room_id')} {ev.get('sender')}: "
                 f"ignored (zero-width marker: written for humans)")
            return
        room_id = ev["room_id"]
        room_name = self._name_for(room_id, ev.get("room_name"))
        where = f"room={room_name or room_id}"

        # Commands may be addressed with leading mentions ("@kraken /status"
        # in a group chat): they parse against the mention-stripped body and
        # only run on the bots the mentions actually point at; a command for
        # another bot is ignored here entirely (before any trigger could see
        # it) instead of being answered as ordinary chat.
        cmd_body, mentioned = strip_leading_mentions(body)
        if _looks_like_command(cmd_body) and not self._command_is_for_me(
                ev, mentioned):
            _log(f"{self.tag} {where} sender={ev.get('sender')} -> command "
                 f"addressed to another bot, staying silent")
            return

        if is_stop_command(cmd_body):
            self._stop_room(room_id, where, ev.get("event_id") or "")
            return

        if parse_help_command(cmd_body):
            self._help_room(room_id)
            return

        if parse_status_command(cmd_body):
            self._status_room(room_id)
            return

        is_mute, mute_args = parse_mute_command(cmd_body)
        if is_mute:
            self._mute_room(room_id, mute_args)
            return

        is_unmute, unmute_args = parse_unmute_command(cmd_body)
        if is_unmute:
            self._unmute_room(room_id, unmute_args)
            return

        if self.bot.mute.expiry(room_id) is not None:
            _log(f"{self.tag} {where} sender={ev.get('sender')} -> ignored "
                 f"(muted in this room)")
            return

        is_effort, level = parse_effort_command(cmd_body)
        if is_effort:
            self._effort_room(room_id, level)
            return

        is_btw, btw_text = parse_btw_command(cmd_body)
        if is_btw:
            self._btw_room(room_id, btw_text, ev)
            return

        if parse_end_command(cmd_body):
            self._end_room(room_id)
            return

        is_new, new_text = parse_new_command(cmd_body)
        if is_new:
            self._new_room(room_id, new_text, ev)
            return

        is_revert, revert_text = parse_revert_command(cmd_body)
        if is_revert:
            self._revert_room(room_id, room_name, revert_text)
            return

        if not addressed_to_bot(ev, self.bot.user_id):
            _log(f"{self.tag} {where} sender={ev.get('sender')} -> ignored "
                 f"(tags someone else, not this bot)")
            return

        sender = (ev.get("sender") or "").casefold()
        if (sender and sender in self._peer_bots
                and not tags_bot_itself(ev, self.bot.user_id)):
            _log(f"{self.tag} {where} sender={ev.get('sender')} -> ignored "
                 f"(sibling bot, this bot not tagged)")
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

        # remember for /revert: compact copy of what a re-fire needs
        with self._session_lock:
            self._fired[key] = {
                "trigger": trig.room,
                "ev": {"room_id": room_id, "room_name": ev.get("room_name"),
                       "event_id": ev.get("event_id"),
                       "sender": ev.get("sender"), "ts": ev.get("ts"),
                       "body": ev.get("body")},
            }
        self._save_room_state()

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
        action = trig.action
        # The room's live session decides how this message is admitted:
        # inside its window the message continues the conversation ({{msg}}
        # then carries only the new message — the session already knows the
        # rest), otherwise a fresh session gets the full history render.
        cont = self._claim_session(room_id)
        if sess.cancelled:
            _log(f"{tag} cancelled before the action started")
            if cont:
                self._remember_session(room_id, str(cont.get("id")),
                                       managed=bool(cont.get("managed")))
            return
        if cont:
            sid = str(cont.get("id"))
            managed = bool(cont.get("managed"))
            with self._session_lock:
                sess.session_ids.append(sid)   # so "stop" can interrupt it
            values = {
                "channel": room_name or room_id,
                "room": room_id,
                "sender": ev.get("sender") or "",
                "msg": format_history([ev]),
            }
            _log(f"{tag} continuing the room's live session {sid}")
            started = time.monotonic()
            result = self.bot.opencode.continue_session(
                sid, prompt=render_text(action.prompt, values),
                timeout=action.timeout, cancel=lambda: sess.cancelled,
                keep_session=True, log=lambda m: _log(f"{tag} {m}"))
        else:
            managed = not action.keep_session
            try:
                history = self.bot.client.read_messages(
                    room_id, limit=trig.context, crypto=self.bot.crypto)
            except MatrixError as exc:
                _log(f"{tag} reading history failed: {exc}")
                history = [ev]
            if not any(m.get("event_id") == ev.get("event_id")
                       for m in history):
                # Sync was first: make sure the triggering message is in
                # context.
                history = [*history, ev]
                history = history[-trig.context:]
            # A re-fired message (/revert) can carry updated text — render
            # the event being handled, not its stale original line.
            history = [dict(m, body=ev["body"])
                       if (m.get("event_id") == ev.get("event_id")
                           and m.get("body") != ev.get("body"))
                       else m
                       for m in history]
            values = {
                "channel": room_name or room_id,
                "room": room_id,
                "sender": ev.get("sender") or "",
                "msg": format_history(history),
            }

            def _note_session(new_sid: str) -> None:
                with self._session_lock:
                    sess.session_ids.append(new_sid)

            started = time.monotonic()
            result = self.bot.opencode.run(
                title=f"MatrixWebhooker {self.bot.name} "
                      f"{room_name or room_id}",
                prompt=render_text(action.prompt, values),
                system=action.system, model=action.model, agent=action.agent,
                directory=action.directory or str(BASE_DIR),
                variant=self.bot.effort.get(room_id),
                timeout=action.timeout, cancel=lambda: sess.cancelled,
                keep_session=True,  # lifetime is decided below
                on_session=_note_session,
                log=lambda m: _log(f"{tag} {m}"))
        dt = time.monotonic() - started
        if result.outcome == "succeeded":
            _log(f"{tag} answered in {dt:.1f}s ({len(result.text)} chars)")
        elif result.outcome == "timeout":
            _log(f"{tag} no answer after {dt:.1f}s (action.timeout)")
        elif result.outcome == "interrupted":
            _log(f"{tag} interrupted after {dt:.1f}s")
        elif result.outcome == "failed":
            _log(f"{tag} action failed after {dt:.1f}s: {result.error} "
                 f"(session {result.session_id} kept for inspection)")
        else:
            _log(f"{tag} run errored after {dt:.1f}s: {result.error}")
        self._post_outcome(room_id, sess, result, action.timeout)
        # Session lifetime (the run itself always keeps, see above): a
        # succeeded run's session stays open as the room's live
        # conversation, a failed one stays for inspection in the TUI,
        # everything else is deleted again — sessions of keep_session
        # actions always stay.
        if result.outcome == "succeeded" and result.session_id:
            self._remember_session(room_id, result.session_id, managed)
        elif result.session_id and managed and result.outcome != "failed":
            self.bot.opencode.delete_session(result.session_id)
        if sess.cancelled:
            _log(f"{tag} stopped by user command")
