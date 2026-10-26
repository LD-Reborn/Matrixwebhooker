"""Answering MatrixRTC (Element Call) calls with speech.

A bot with ``"call": {"enabled": true, "action": {…}}`` in ``bots.json``
joins a call that someone else started in a room it is in:

  1. the watcher hands us the room's changed call-membership state events
     (``org.matrix.msc3401.call.member`` / ``org.matrix.msc4143.rtc.member``);
  2. we publish *our own* membership, mirroring the shape the caller used
     (that keeps us compatible with whatever Element Call version is out
     there), and ask the homeserver's JWT service for a LiveKit token;
  3. we join the LiveKit SFU, listen to the other participants, cut their
     speech into utterances (energy VAD), send each utterance to whisper,
     run the configured action on the OpenCode server with the transcript
     and speak the answer back through the TTS gateway;
  4. when the last other membership disappears (or the slot closes, or a bare
     ``stop`` arrives, or ``max_seconds`` elapse) we withdraw our membership
     and leave.

The bot can also *place* the call itself (``CallManager.place_call``, behind
``POST /call`` and the MCP ``place_call`` tool): we publish our own
membership (the legacy session shape current Element clients use — empty
``call_id``, which is the same SFU slot calls in these rooms land in), send
each other room member an MSC4184 ``m.call.notify`` to-device event so their
Element client rings, and wait up to ``ring_seconds``.  When they pick up we
speak the ``note`` (or the greeting); when nobody does we leave and post
``no_answer_message`` to the room.

The media side needs the optional ``livekit`` package; without it the module
still imports and the bot just reports that it cannot answer calls.
"""

from __future__ import annotations

import array
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path

from matrixbot import (BASE_DIR, CALL_STATE_TYPES, Bot, CallConfig, MatrixError)
from speech import Speech, SpeechConfig, SpeechError, resample
from triggers import format_history, render_text

#: sample rate we publish at (LiveKit's native Opus rate)
OUT_RATE = 48000
FRAME_MS = 20
#: seconds of audio kept ahead of an utterance, so an interruption that starts
#: while we are still talking does not lose its first words
PRE_ROLL = 0.8


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# membership events: mirror what the caller sent
# ---------------------------------------------------------------------------


def slot_id_of(template: dict) -> str:
    """The MatrixRTC slot the caller's membership sits in (``m.call#<call_id>``).

    The SFU room name is derived from this, so an invented call id puts us in a
    different LiveKit room than the caller.
    """
    content = template.get("content") or {}
    member = content.get("member")
    if isinstance(member, dict) and isinstance(member.get("slot_id"), str):
        return member["slot_id"]
    if isinstance(content.get("slot_id"), str):
        return content["slot_id"]
    application = content.get("application")
    app = application if isinstance(application, str) else "m.call"
    # legacy sessions with an empty call id sit in the slot "m.call#ROOM"
    return f"{app}#{content.get('call_id') or 'ROOM'}"


def is_sticky(content: dict) -> bool:
    """True for the MSC4143 membership shape (``content.member``)."""
    return isinstance(content.get("member"), dict)


def own_membership(template: dict, *, user_id: str, device_id: str, member_id: str,
                   sfu_url: str = "", service_url: str = "") -> tuple[str, dict]:
    """Our membership event, mirroring the caller's whole event.

    Returns ``(state_key, content)``.  Only the identity fields are swapped —
    application, call id, scope, intent, foci and expiry are copied, and the
    state key keeps the caller's shape (current Element Call uses
    ``_@user_device_m.call``, not the bare user id).  That matters: Element
    Call matches LiveKit participants against ``membershipID``/the state key,
    so a copied or mismatched identity means the other side never hears us.
    """
    content = json.loads(json.dumps(template.get("content") or {}))
    sender = template.get("sender") or ""
    their_device = content.get("device_id") or ""

    for key in ("org.matrix.msc4140.delayed_action",
                "org.matrix.msc4140.delayed_action_timeout_ms",
                "org.matrix.msc4140.delayed_action_id",
                "msc4354_sticky_key"):
        content.pop(key, None)

    if is_sticky(content):
        member = dict(content.get("member") or {})
        member["id"] = member_id
        member["claimed_user_id"] = user_id
        member["claimed_device_id"] = device_id
        member["device_id"] = device_id
        member["membership"] = "join"
        member.pop("foci_preferred", None)
        content["member"] = member
        return member_id, content

    # MSC3401 session membership
    content["application"] = content.get("application") or "m.call"
    content.setdefault("call_id", "ROOM")
    content["device_id"] = device_id
    if "membershipID" in content or str(template.get("state_key") or "").startswith("_"):
        content["membershipID"] = member_id          # the identity we join with
    if not content.get("foci_preferred"):
        if sfu_url:
            content["foci_preferred"] = [{"type": "livekit", "url": sfu_url}]
        elif service_url:
            content["foci_preferred"] = [{"livekit_service_url": service_url}]

    key = str(template.get("state_key") or "")
    if not key or key == sender:
        key = user_id
    else:
        if sender:
            key = key.replace(sender, user_id)
        if their_device:
            key = key.replace(their_device, device_id)
    return key, content


def left_membership(template: dict, *, user_id: str, device_id: str,
                    member_id: str) -> tuple[str, dict]:
    """The withdrawal event that ends our participation (same state key)."""
    content = template.get("content") or {}
    if is_sticky(content):
        return member_id, {"member": {
            "membership": "leave", "id": member_id,
            "slot_id": slot_id_of(template),
            "claimed_user_id": user_id, "claimed_device_id": device_id}}
    key, _ = own_membership(template, user_id=user_id, device_id=device_id,
                            member_id=member_id)
    return key, {}


def outgoing_template(client) -> tuple[dict, list[dict]]:
    """The membership event we publish when *we* place a call.

    Shaped like the sessions current Element clients create themselves
    (MSC3401 legacy: state key ``_<user>_<device>_m.call``, empty
    ``call_id``, ``membershipID`` = ``userId:deviceId``) — that is exactly
    the shape whose SFU slot (the JWT service's default, ``m.call#ROOM``)
    calls in these rooms land in today, so a picking-up client meets us in
    the same LiveKit room.  Returns ``(template, foci_preferred)``.
    """
    user_id = client.user_id
    device_id = client.device_id or ""
    transports = client.rtc_transports()
    service_url = next((t.get("livekit_service_url") for t in transports
                        if t.get("livekit_service_url")), "")
    foci = ([{"livekit_service_url": service_url}] if service_url
            else [{"type": "livekit", "url": t["url"]}
                  for t in transports if t.get("url")])
    content = {
        "application": "m.call",
        "call_id": "",                       # the room's default session slot
        "device_id": device_id,
        "expires": 14400000,                 # 4 h, like Element Call
        "foci_preferred": foci,
        "focus_active": {"focus_selection": "multi_sfu", "type": "livekit"},
        "m.call.intent": "audio",
        "membershipID": f"{user_id}:{device_id}",
        "scope": "m.room",
    }
    template = {
        "type": "org.matrix.msc3401.call.member",
        "state_key": f"_{user_id}_{device_id}_m.call",
        "sender": user_id,
        "content": content,
    }
    return template, foci


def _rms(data: bytes) -> float:
    samples = array.array("h", data)
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


def clip_for_speech(text: str, limit: int = 220) -> str:
    """Shorten an answer at a sentence boundary.

    Measured on the gateway: tts takes ~3.5x the length of what it speaks and
    does not stream, so the caller waits that long before hearing anything.
    The length of the answer *is* the latency - hence a tight default.
    """
    if len(text) <= limit:
        return text
    head = text[:limit]
    for sep in (". ", "! ", "? ", ".\n", "\n"):
        cut = head.rfind(sep)
        if cut > limit // 2:
            return head[:cut + 1].strip()
    cut = head.rfind(" ")
    return (head[:cut] if cut > limit // 2 else head).strip() + " …"


# ---------------------------------------------------------------------------
# one live call
# ---------------------------------------------------------------------------


class CallSession:
    """A single answered call in one room (runs its own asyncio loop)."""

    def __init__(self, manager: "CallManager", room_id: str, room_name: str | None,
                 template: dict, *, outgoing: bool = False, note: str = "",
                 invite: list[str] | None = None, foci: list[dict] | None = None,
                 ring_seconds: int = 0):
        """``template`` is the caller's *whole* membership event: we mirror it.

        For an outgoing call the "template" is our own freshly built
        membership (``outgoing_template``); we then ring ``invite`` via
        MSC4184 to-device notifications and speak ``note`` once they pick
        up (instead of the plain greeting).
        """
        self.mgr = manager
        self.bot: Bot = manager.bot
        self.cfg: CallConfig = manager.cfg
        self.room_id = room_id
        self.room_name = room_name
        self.template = template
        self.template_type = template.get("type") or "org.matrix.msc3401.call.member"
        self.sticky = is_sticky(template.get("content") or {})
        self.outgoing = outgoing
        self.note = note
        self.invite = invite or []
        self.foci = foci or []
        self.ring_seconds = ring_seconds or self.cfg.ring_seconds
        device_id = self.bot.client.device_id or ""
        #: the identity we join the SFU with *and* publish as membershipID —
        #: legacy Element Call expects "userId:deviceId" (what its own /sfu/get
        #: token carries), MSC4143 clients expect a per-member id
        self.member_id = ("mw-" + secrets.token_hex(8)) if self.sticky \
            else f"{self.bot.user_id}:{device_id}"
        self.started = time.monotonic()
        self.stopping = threading.Event()
        self.utterances = 0
        self.answered = not outgoing    # an answered call counts as picked up
        self.rang_out = False           # outgoing, nobody picked up
        # utterance assembly (energy VAD)
        self._buf = bytearray()
        self._speech = 0.0
        self._silence = 0.0
        self._loud = 0.0
        self._pre = bytearray()           # audio kept ahead of an utterance
        self._in_speech = False
        self._echo_until = 0.0          # half duplex: damp input while we speak
        self._interrupted = False       # the caller cut us off
        self._skip_leave = False        # replaced by a newer session
        self._timed_out = False         # the answer action ran past its timeout
        self._source = None
        self._thread: threading.Thread | None = None
        self.tag = f"[call:{self.bot.name}:{room_id[:14]}]"

    # -- lifecycle --------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._guard,
                                        name=f"call-{self.room_id[:12]}", daemon=True)
        self._thread.start()

    def stop(self, reason: str = "stopped") -> None:
        if not self.stopping.is_set():
            _log(f"{self.tag} ending call: {reason}")
        self.stopping.set()

    def abandon(self) -> None:
        """Stop without withdrawing our membership: a new session for the same
        room has already published it, and our leave would undo that."""
        self._skip_leave = True
        self.stop("replaced by a new call")

    def _guard(self) -> None:
        import asyncio
        try:
            asyncio.run(self._run())
        except Exception as exc:  # noqa: BLE001 - a call must never kill the bot
            _log(f"{self.tag} call failed: {exc!r}")
        finally:
            self.stopping.set()
            self.mgr.session_finished(self)

    # -- the call ---------------------------------------------------------------

    async def _run(self) -> None:
        import asyncio

        from livekit import rtc

        if self.mgr.speech is None:
            _log(f"{self.tag} no VOICE_BASE_URL — cannot answer")
            return

        transports = self.bot.client.rtc_transports()
        service_url = next((t.get("livekit_service_url") for t in transports
                            if t.get("livekit_service_url")), "")
        slot = slot_id_of(self.template)
        token = self.bot.client.livekit_token(
            self.room_id, slot, self.member_id if self.sticky else None)
        sfu_url = token.get("url") or ""
        if not sfu_url or not token.get("jwt"):
            _log(f"{self.tag} the JWT service gave no usable token: {token}")
            return

        key, content = own_membership(
            self.template, user_id=self.bot.user_id,
            device_id=self.bot.client.device_id or "", member_id=self.member_id,
            sfu_url=sfu_url, service_url=service_url)
        try:
            resp = self.bot.client.send_state(self.room_id, self.template_type,
                                              key, content)
        except MatrixError as exc:
            _log(f"{self.tag} publishing call membership failed: {exc} "
                 "(the bot needs power level >= 50 in this room)")
            return
        _log(f"{self.tag} joined {self.template_type} slot {slot} as {self.member_id} "
             f"(state key {key!r}) via {sfu_url}")
        if self.outgoing:
            self._ensure_slot_open()
            self._send_call_notify((resp or {}).get("event_id") or "")
            self._post_call_message()

        room = rtc.Room()
        source = rtc.AudioSource(OUT_RATE, 1)
        self._source = source
        track = rtc.LocalAudioTrack.create_audio_track("mw-voice", source)
        incoming: asyncio.Queue = asyncio.Queue()

        @room.on("track_subscribed")
        def _(tr, publication, participant):
            if tr.kind == rtc.TrackKind.KIND_AUDIO:
                self.answered = True
                incoming.put_nowait((tr, participant.identity))

        try:
            await room.connect(sfu_url, token["jwt"])
            _log(f"{self.tag} livekit room {room.name}: "
                 f"{[p.identity for p in room.remote_participants.values()]}")
            if room.remote_participants:
                self.answered = True
            await room.local_participant.publish_track(track)

            tasks = [asyncio.create_task(self._pump_incoming(incoming)),
                     asyncio.create_task(self._refresh_membership()),
                     asyncio.create_task(self._greet())]
            deadline = self.started + self.cfg.max_seconds
            ring_deadline = self.started + self.ring_seconds
            while not self.stopping.is_set():
                if time.monotonic() > deadline:
                    _log(f"{self.tag} max_seconds reached")
                    break
                if self.outgoing and not self.answered and time.monotonic() > ring_deadline:
                    _log(f"{self.tag} nobody picked up in {self.ring_seconds}s")
                    self.rang_out = True
                    break
                await asyncio.sleep(0.5)
            for t in tasks:
                t.cancel()
        finally:
            if not self._skip_leave:
                try:
                    key, leave = left_membership(
                        self.template, user_id=self.bot.user_id,
                        device_id=self.bot.client.device_id or "",
                        member_id=self.member_id)
                    self.bot.client.send_state(self.room_id, self.template_type, key, leave)
                except MatrixError as exc:
                    _log(f"{self.tag} withdrawing membership failed: {exc}")
                if self.outgoing:
                    self._close_slot_if_alone()
            try:
                await room.disconnect()
            except Exception:  # noqa: BLE001
                pass
            _log(f"{self.tag} left ({self.utterances} utterance(s) handled)")
            if self.rang_out and self.cfg.no_answer_message:
                try:
                    self.bot.client.send_message(
                        self.room_id, {"msgtype": "m.notice",
                                       "body": self.cfg.no_answer_message})
                except MatrixError as exc:
                    _log(f"{self.tag} posting the no-answer note failed: {exc}")

    def _send_call_notify(self, event_id: str) -> None:
        """Ring the invitees.

        Current clients (Element X included) follow MSC4075: a room event
        ``org.matrix.msc4075.rtc.notification`` mentioning the room — the
        receiver only accepts it when the slot state exists as ``open``
        (see ``_ensure_slot_open``), it mentions them, and its lifetime
        (max 2 minutes) has not elapsed. Older clients understood the
        MSC4184 ``m.call.notify`` to-device event instead (focus entries
        need the ``type: livekit`` discriminant there, or rust-sdk clients
        silently drop it); both are sent, the stranger is ignored.
        """
        if not event_id:
            _log(f"{self.tag} membership event has no id — cannot ring anyone")
            return
        if not self.invite:
            _log(f"{self.tag} nobody else in the room to ring")
            return
        slot = slot_id_of(self.template)
        try:
            self.bot.client.send_event(self.room_id, "org.matrix.msc4075.rtc.notification", {
                "slot_id": slot,
                "m.mentions": {"user_ids": [], "room": True},
                "m.relates_to": {"rel_type": "m.reference", "event_id": event_id},
                "sender_ts": int(time.time() * 1000),
                "lifetime": min(self.ring_seconds, 120) * 1000,
                "msc4354_sticky_key": slot,
                # deprecated fields current senders still include for old receivers:
                "notification_type": "ring",
                "m.call.intent": "audio",
            })
        except MatrixError as exc:
            _log(f"{self.tag} msc4075 room notification failed: {exc}")
        call_id = str((self.template.get("content") or {}).get("call_id", ""))
        foci = [{"type": "livekit", **{k: v for k, v in f.items() if k != "type"}}
                for f in self.foci]
        for user in self.invite:
            content = {
                "invitee": {"user_id": user},
                "event_id": event_id,
                "room_id": self.room_id,
                "call_id": call_id,
                "foci_preferred": foci,
                "reason": "user_invite",
            }
            for etype in ("org.matrix.msc4184.call.notify", "m.call.notify"):
                try:
                    self.bot.client.send_to_device(etype, {user: {"*": content}})
                except MatrixError as exc:
                    _log(f"{self.tag} {etype} to {user} failed: {exc}")
            _log(f"{self.tag} rang {user} (room notification + to-device)")

    def _ensure_slot_open(self) -> None:
        """The MSC4143 slot state has to read 'open' or receivers reject the
        notification ("slot does not exist or is closed")."""
        slot = slot_id_of(self.template)
        try:
            self.bot.client.send_state(self.room_id, "org.matrix.msc4143.rtc.slot",
                                       slot, {"status": "open",
                                              "application": {"type": "m.call"}})
        except MatrixError as exc:
            _log(f"{self.tag} opening call slot {slot!r} failed: {exc}")

    def _close_slot_if_alone(self) -> None:
        """Outgoing call over and nobody else is in the slot: close it again
        (leave the slot alone if the callee stayed on the call)."""
        if self.mgr._members.get(self.room_id):
            return
        slot = slot_id_of(self.template)
        try:
            self.bot.client.send_state(self.room_id, "org.matrix.msc4143.rtc.slot",
                                       slot, {"status": "closed",
                                              "application": {"type": "m.call"}})
        except MatrixError as exc:
            _log(f"{self.tag} closing call slot {slot!r} failed: {exc}")

    def _post_call_message(self) -> None:
        """Visible timeline fallback for the outgoing call: clients that do
        not ring for ``m.call.notify`` can still join via the call link."""
        if not self.cfg.call_message:
            return
        link = f"https://call.element.io/#/room/{self.room_id}"
        body = render_text(self.cfg.call_message, {"link": link})
        try:
            self.bot.client.send_message(self.room_id, {
                "msgtype": "m.text", "body": body,
                "format": "org.matrix.custom.html",
                "formatted_body": body.replace(link, f'<a href="{link}">{link}</a>'),
            })
        except MatrixError as exc:
            _log(f"{self.tag} posting the call message failed: {exc}")

    async def _refresh_membership(self) -> None:
        """Re-PUT our membership so it never looks stale to other clients."""
        import asyncio
        while not self.stopping.is_set():
            await asyncio.sleep(120)
            try:
                key, content = own_membership(
                    self.template, user_id=self.bot.user_id,
                    device_id=self.bot.client.device_id or "", member_id=self.member_id)
                self.bot.client.send_state(self.room_id, self.template_type, key, content)
            except Exception as exc:  # noqa: BLE001
                _log(f"{self.tag} membership refresh failed: {exc}")

    async def _greet(self) -> None:
        """Say hello right away — the caller can simply talk over us.

        This used to wait 1.5 s and then synthesize, which took a few more
        seconds: callers had already asked their question and then heard the
        greeting *after* it.  The audio is cached, so it starts at once and
        barge-in cuts it off.

        Outgoing calls are the mirror case: we wait until the callee has
        actually picked up, then speak ``note`` (an alert's message — why we
        are calling) if given, else the greeting.
        """
        import asyncio
        if self.outgoing:
            while not self.stopping.is_set() and not self.answered:
                await asyncio.sleep(0.5)
            if self.stopping.is_set() or not self.answered:
                return
            text = self.note or self.cfg.greeting
            if not text:
                return
            if self.utterances or self._in_speech or self._speech:
                _log(f"{self.tag} greeting skipped (the callee already spoke)")
                return
            if self.note:
                text = clip_for_speech(text, self.cfg.max_answer_chars)
            await self._speak(text, cached=not self.note)
            return
        if not self.cfg.greeting:
            return
        await asyncio.sleep(0.3)
        if self.stopping.is_set() or self.utterances or self._in_speech or self._speech:
            _log(f"{self.tag} greeting skipped (the caller already spoke)")
            return
        await self._speak(self.cfg.greeting, cached=True)

    # -- listening ---------------------------------------------------------------

    async def _pump_incoming(self, incoming: asyncio.Queue) -> None:
        """Read every subscribed audio track and cut it into utterances."""
        import asyncio

        from livekit import rtc

        while not self.stopping.is_set():
            try:
                track, who = await asyncio.wait_for(incoming.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            _log(f"{self.tag} audio track from {who}")
            try:
                stream = rtc.AudioStream(track, sample_rate=OUT_RATE, num_channels=1)
                async for event in stream:
                    if self.stopping.is_set():
                        break
                    self._feed(event.frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _log(f"{self.tag} audio stream from {who} ended: {exc!r}")

    def _feed(self, frame) -> None:
        """Energy VAD: buffer speech, hand complete utterances to the agent.

        While we speak ourselves the input is damped, not thrown away: our own
        voice comes back through the caller's microphone, usually quieter than
        the caller.  Anything clearly louder than that for ``barge_in`` seconds
        is the caller interrupting us — we stop talking and start listening.
        """
        import asyncio

        dur = frame.samples_per_channel / OUT_RATE
        gated = time.monotonic() < self._echo_until
        threshold = self.cfg.vad_threshold * (self.cfg.echo_factor if gated else 1.0)

        # keep a little audio ahead of every utterance: a caller who starts
        # talking while we are still speaking would otherwise lose their first
        # words, and that is exactly when they are interrupting us
        self._pre.extend(frame.data)
        keep = int(OUT_RATE * PRE_ROLL) * 2
        if len(self._pre) > keep:
            del self._pre[:len(self._pre) - keep]

        if _rms(frame.data) >= threshold:
            if gated:
                self._loud += dur
                if self._loud < self.cfg.barge_in:
                    return                    # still most likely our own echo
                _log(f"{self.tag} caller interrupted us")
                self._echo_until = 0.0
                self._interrupt()
            self._in_speech = True
            self._speech += dur
            self._silence = 0.0
            if not self._buf:
                self._buf.extend(self._pre)
            self._buf.extend(frame.data)
            if self._speech >= self.cfg.max_utterance:
                _log(f"{self.tag} utterance hit max_utterance; cutting")
                self._flush()
            return

        self._loud = 0.0
        if not self._in_speech:
            return
        self._buf.extend(frame.data)            # keep the trailing word
        self._silence += dur
        if self._silence >= self.cfg.silence_end:
            self._flush()

    def _interrupt(self) -> None:
        """Stop our own playback (the caller is talking over us)."""
        self._interrupted = True
        if self._source is not None:
            try:
                self._source.clear_queue()
            except Exception:  # noqa: BLE001
                pass

    def _flush(self) -> None:
        """Hand the buffered utterance to the answering pipeline."""
        import asyncio

        pcm, speech = bytes(self._buf), self._speech
        self._buf = bytearray()
        self._in_speech, self._speech = False, 0.0
        self._silence = self._loud = 0.0
        if speech < self.cfg.min_speech or len(pcm) < OUT_RATE // 4:
            return
        _log(f"{self.tag} utterance {speech:.1f}s -> stt")
        asyncio.create_task(self._answer(pcm))

    # -- answering ----------------------------------------------------------------

    async def _answer(self, pcm: bytes) -> None:
        """stt -> action -> tts -> publish, one utterance at a time."""
        import asyncio

        loop = asyncio.get_running_loop()
        t_start = time.monotonic()
        try:
            text = await loop.run_in_executor(
                None, lambda: self.mgr.speech.transcribe(pcm, OUT_RATE, self.cfg.language))
        except SpeechError as exc:
            _log(f"{self.tag} {exc}")
            return
        if not text:
            return
        _log(f"{self.tag} heard: {text[:120]}")
        self.utterances += 1
        fut = loop.run_in_executor(None, self._run_action, text)
        if self.cfg.thinking_cue:
            done, _ = await asyncio.wait({fut}, timeout=self.cfg.cue_after)
            if not done and not self.stopping.is_set():
                await self._speak(self.cfg.thinking_cue, cached=True)
        answer = await fut
        _log(f"{self.tag} agent answered in {time.monotonic() - t_start:.1f}s")
        if answer:
            spoken = clip_for_speech(answer, self.cfg.max_answer_chars)
            if spoken != answer:
                _log(f"{self.tag} answer trimmed for speech: "
                     f"{len(answer)} -> {len(spoken)} chars")
            await self._speak(spoken, t0=t_start)
        elif self._timed_out and self.cfg.sorry_line:
            # silence is worse than an apology: the caller is still holding
            await self._speak(self.cfg.sorry_line, cached=True, t0=t_start)
        if self.cfg.log_transcripts:
            await loop.run_in_executor(None, self._log_to_room, text, answer)

    def _log_to_room(self, heard: str, answer: str) -> None:
        """Keep a written record of the call in the room itself."""
        body = f"📞 {heard}" + (f"\n🤖 {answer}" if answer else "")
        try:
            self.bot.client.send_message(self.room_id, {"msgtype": "m.text", "body": body})
        except MatrixError as exc:
            _log(f"{self.tag} writing the call record to the room failed: {exc}")

    async def _speak(self, text: str, cached: bool = False,
                     t0: float = 0.0) -> None:
        """Speak ``text``.

        ``t0`` (a time.monotonic reading for when the caller finished speaking)
        is only used to log how long the answer took to start.

        One request per answer, deliberately: the tts backend serializes (two
        concurrent requests take exactly as long as two in a row), so
        splitting an answer only moves the wait into the middle of the
        sentence.  What helps is saying less - see ``max_answer_chars``.
        """
        import asyncio

        from livekit import rtc

        if self._source is None:
            return
        loop = asyncio.get_running_loop()
        try:
            if cached:
                pcm = await loop.run_in_executor(
                    None, lambda: self.mgr.speech.cached(text, OUT_RATE,
                                                         self.cfg.language))
            else:
                pcm, rate = await loop.run_in_executor(
                    None, lambda: self.mgr.speech.synthesize(
                        text, language=self.cfg.language or None))
                pcm = resample(pcm, rate, OUT_RATE)
        except SpeechError as exc:
            _log(f"{self.tag} {exc}")
            return
        seconds = len(pcm) / 2 / OUT_RATE
        note = f" (+{time.monotonic() - t0:.1f}s after the caller)" if t0 else ""
        _log(f"{self.tag} speaking {seconds:.1f}s: {text[:100]}{note}")
        self._interrupted = False
        self._echo_until = time.monotonic() + seconds + 0.5
        size = int(OUT_RATE * FRAME_MS / 1000) * 2
        for i in range(0, len(pcm), size):
            if self.stopping.is_set():
                return
            if self._interrupted:
                _log(f"{self.tag} stopped speaking after {i / 2 / OUT_RATE:.1f}s "
                     "(the caller interrupted)")
                return
            chunk = pcm[i:i + size]
            await self._source.capture_frame(
                rtc.AudioFrame(chunk, OUT_RATE, 1, len(chunk) // 2))
            while self._source.queued_duration > 1000 and not self._interrupted:
                await asyncio.sleep(0.05)

    def _run_action(self, transcript: str) -> str:
        """Run the configured action; its final assistant text is the answer."""
        action = self.cfg.action
        if action is None:
            _log(f"{self.tag} call enabled but 'call.action' is not configured")
            return ""
        try:
            history = self.bot.client.read_messages(self.room_id, limit=self.cfg.context,
                                                    crypto=self.bot.crypto)
        except MatrixError as exc:
            _log(f"{self.tag} reading history failed: {exc}")
            history = []
        new_message = {"sender": "you", "ts": int(time.time() * 1000), "body": transcript}
        values = {
            "channel": self.room_name or self.room_id,
            "room": self.room_id,
            "sender": "you",
            "msg": format_history([*history, new_message]),
            "transcript": transcript,
        }
        self._timed_out = False
        result = self.bot.opencode.run(
            title=f"MatrixWebhooker call {self.bot.name} {self.room_name or self.room_id}",
            prompt=render_text(action.prompt, values),
            system=action.system, model=action.model, agent=action.agent,
            directory=action.directory or str(BASE_DIR),
            variant=self.bot.effort.get(self.room_id),
            timeout=action.timeout, cancel=lambda: self.stopping.is_set(),
            keep_session=action.keep_session,
            log=lambda m: _log(f"{self.tag} {m}"))
        if result.outcome == "timeout":
            self._timed_out = True
        elif result.outcome == "succeeded":
            return result.text
        elif result.outcome != "interrupted":
            _log(f"{self.tag} action failed: {result.error} "
                 f"(session {result.session_id} kept)")
        return ""


# ---------------------------------------------------------------------------
# per-bot manager
# ---------------------------------------------------------------------------


class CallManager:
    """Watches a bot's rooms for calls and keeps one CallSession per room."""

    def __init__(self, bot: Bot, cfg: CallConfig, on_log=None):
        self.bot = bot
        self.cfg = cfg
        self.on_log = on_log or _log
        speech_cfg = SpeechConfig.from_env()
        if speech_cfg:
            cache = os.environ.get("VOICE_CACHE_DIR", "").strip()
            self.speech = Speech(speech_cfg, on_log=on_log,
                                 cache_dir=cache or Path(bot.client.state_dir)
                                                     / "voice-cache")
        else:
            self.speech = None
        self._sessions: dict[str, CallSession] = {}
        self._members: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def prewarm(self) -> None:
        """Synthesize the fixed phrases once, in the background.

        The greeting and the "one moment" cue are the two things that have to
        be ready *before* the caller says anything; paying for them at join
        time costs seconds of dead air and puts the greeting after the
        question it is answering.
        """
        if self.speech is None:
            return
        phrases = [p for p in (self.cfg.greeting, self.cfg.thinking_cue,
                               self.cfg.sorry_line) if p]

        def _warm() -> None:
            for phrase in phrases:
                try:
                    self.speech.cached(phrase, OUT_RATE, self.cfg.language)
                except SpeechError as exc:
                    self.on_log(f"[call:{self.bot.name}] prewarm failed: {exc}")

        if phrases:
            threading.Thread(target=_warm, daemon=True,
                             name=f"call-prewarm-{self.bot.name}").start()

    # -- entry points ------------------------------------------------------------

    def on_state_event(self, ev: dict) -> None:
        """Feed one changed room-state event (from /sync) into the manager."""
        if not self.cfg.enabled or ev.get("type") not in CALL_STATE_TYPES:
            return
        room_id = ev.get("room_id") or ""
        sender = ev.get("sender")
        if not room_id or sender == self.bot.user_id:
            return
        if not self.cfg.matches(room_id, ev.get("room_name")):
            return

        if ev["type"] == "org.matrix.msc4143.rtc.slot":
            if (ev.get("content") or {}).get("status") == "closed":
                self.stop_room(room_id, "call slot closed")
            return

        content = ev.get("content") or {}
        left = not content or (is_sticky(content)
                               and content["member"].get("membership") == "leave")
        # keyed by state key, not sender: that identifies one device's seat in
        # the call, so a second device keeps the call alive
        seat = ev.get("state_key") or sender or ""
        if left:
            self._members.setdefault(room_id, set()).discard(seat)
            if not self._members[room_id]:
                self.stop_room(room_id, "everyone left")
            return

        self._members.setdefault(room_id, set()).add(seat)
        self.join(room_id, ev.get("room_name"), ev)

    def poll_rooms(self) -> None:
        """Find calls that are already running (e.g. after a restart)."""
        if not self.cfg.enabled:
            return
        try:
            rooms = self.bot.client.list_rooms()
        except MatrixError as exc:
            self.on_log(f"[call:{self.bot.name}] listing rooms failed: {exc}")
            return
        for room in rooms:
            room_id = room["room_id"]
            if not self.cfg.matches(room_id, room.get("name")):
                continue
            try:
                events = self.bot.client.room_state(room_id, CALL_STATE_TYPES)
            except MatrixError:
                continue
            for ev in events:
                self.on_state_event({"room_id": room_id, "room_name": room.get("name"),
                                     "type": ev.get("type"),
                                     "state_key": ev.get("state_key"),
                                     "sender": ev.get("sender"),
                                     "content": ev.get("content") or {}})

    def join(self, room_id: str, room_name: str | None, template: dict) -> None:
        """Answer the call whose membership event is ``template``."""
        with self._lock:
            current = self._sessions.get(room_id)
            if current is not None:
                if not current.stopping.is_set():
                    return
                # the call was re-opened while we were still hanging up the old
                # one (leave + join land in the same sync batch): replace it
                self._sessions.pop(room_id, None)
                current.abandon()
            session = CallSession(self, room_id, room_name, template)
            self._sessions[room_id] = session
        if self.speech is None:
            self.on_log(f"[call:{self.bot.name}] VOICE_BASE_URL is not set — "
                        "cannot answer calls")
            self._drop(room_id)
            return
        try:
            import livekit  # noqa: F401 - optional dependency
        except ImportError:
            self.on_log(f"[call:{self.bot.name}] the 'livekit' package is missing — "
                        "cannot answer calls (pip install livekit)")
            self._drop(room_id)
            return
        self.on_log(f"[call:{self.bot.name}] call in {room_name or room_id}: joining")
        session.start()

    def place_call(self, room_id: str, room_name: str | None = None, *,
                   note: str = "", invite: list[str] | None = None,
                   ring_seconds: int = 0) -> str | None:
        """Place an outgoing call in ``room_id`` (the bot publishes the call
        and rings the room's other members).  Returns an error string, or
        ``None`` once the call is under way."""
        if not self.cfg.enabled:
            return "calls are not enabled for this bot (bots.json: \"call\")"
        if self.speech is None:
            return "VOICE_BASE_URL is not set — cannot place calls"
        try:
            import livekit  # noqa: F401 - optional dependency
        except ImportError:
            return "the 'livekit' package is missing (pip install livekit)"
        with self._lock:
            current = self._sessions.get(room_id)
            if current is not None and not current.stopping.is_set():
                return "a call is already running in this room"
        # joined_members also forces the login (we need the device_id first)
        try:
            members = self.bot.client.joined_members(room_id)
        except MatrixError:
            try:
                self.bot.client.join(room_id)
                members = self.bot.client.joined_members(room_id)
            except MatrixError as exc:
                return f"cannot read the room's members: {exc}"
        targets = [m for m in (invite or members) if m != self.bot.user_id]
        template, foci = outgoing_template(self.bot.client)
        session = CallSession(self, room_id, room_name, template, outgoing=True,
                              note=note, invite=targets, foci=foci,
                              ring_seconds=ring_seconds)
        with self._lock:
            current = self._sessions.get(room_id)
            if current is not None and not current.stopping.is_set():
                return "a call is already running in this room"
            self._sessions[room_id] = session
        self.on_log(f"[call:{self.bot.name}] placing call in {room_name or room_id}: "
                    f"ringing {', '.join(targets) or '(nobody else in the room)'}")
        session.start()
        return None

    def _drop(self, room_id: str) -> None:
        with self._lock:
            self._sessions.pop(room_id, None)

    def stop_room(self, room_id: str, reason: str = "stopped") -> None:
        with self._lock:
            session = self._sessions.get(room_id)
        if session:
            session.stop(reason)

    def stop_all(self, reason: str = "shutdown") -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for s in sessions:
            s.stop(reason)

    def status(self) -> list[dict]:
        with self._lock:
            sessions = list(self._sessions.values())
        return [{"room": s.room_id, "seconds": round(time.monotonic() - s.started),
                 "utterances": s.utterances, "outgoing": s.outgoing,
                 "answered": s.answered} for s in sessions]

    def session_finished(self, session: CallSession) -> None:
        with self._lock:
            if self._sessions.get(session.room_id) is session:
                self._sessions.pop(session.room_id, None)
