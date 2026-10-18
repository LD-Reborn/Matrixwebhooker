"""End-to-end self test for the voice-call path — no Element involved.

Runs both sides of a call inside one process, in a throw-away room the bot
creates itself (so your real DMs stay untouched):

  * the *bot* side is the production code: ``CallManager.join`` publishes our
    MatrixRTC membership, asks the homeserver's JWT service for a LiveKit
    token, joins the SFU, listens, runs the command and speaks the answer;
  * the *caller* side is this script: it joins the same LiveKit room, speaks
    one question (TTS), records what comes back and transcribes it.

    python3 _call_selftest.py                       # canned question + canned answer
    python3 _call_selftest.py --question "Wer bist du?" \
        --command 'echo "Ich bin der Testbot."'
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

from calls import OUT_RATE, CallConfig, CallManager      # noqa: E402
from matrixbot import Bot, load_settings                  # noqa: E402
from speech import Speech, SpeechConfig, resample         # noqa: E402

ROOM_NAME = "MatrixWebhooker call selftest"

#: the membership we hand the manager as "what the caller sent" — shaped like
#: current Element Call (state key `_<user>_<device>_<application>`, empty
#: call id, membershipID = the LiveKit identity)
TEMPLATE_EVENT = {
    "type": "org.matrix.msc3401.call.member",
    "state_key": "_@selftest:matrix.example.com_SELFCALL_m.call",
    "sender": "@selftest:matrix.example.com",
    "content": {
        "application": "m.call",
        "call_id": "",
        "device_id": "SELFCALL",
        "expires": 14400000,
        "foci_preferred": [{"type": "livekit",
                            "livekit_service_url": "https://matrix.example.com/livekit/jwt"}],
        "focus_active": {"focus_selection": "multi_sfu", "type": "livekit"},
        "m.call.intent": "audio",
        "membershipID": "@selftest:matrix.example.com:SELFCALL",
        "scope": "m.room",
    },
}


async def run_caller(bot: Bot, room_id: str, question: str, seconds: float,
                     wav_out: Path, language: str = "") -> None:
    """Speak one question into the SFU and record the answer."""
    from livekit import rtc

    speech = Speech(SpeechConfig.from_env())
    pcm, rate = speech.synthesize(question)
    pcm = resample(pcm, rate, OUT_RATE)
    print(f"caller: asking {len(pcm) / 2 / OUT_RATE:.1f}s of audio")

    token = bot.client.livekit_token(room_id, "m.call#")      # legacy /sfu/get
    room = rtc.Room()
    source = rtc.AudioSource(OUT_RATE, 1)
    track = rtc.LocalAudioTrack.create_audio_track("caller", source)
    recorded = bytearray()

    @room.on("track_subscribed")
    def _(tr, publication, participant):
        if tr.kind == rtc.TrackKind.KIND_AUDIO:
            asyncio.create_task(_record(rtc, tr, recorded))

    async def _record(rtc, tr, sink: bytearray):
        stream = rtc.AudioStream(tr, sample_rate=OUT_RATE, num_channels=1)
        async for event in stream:
            sink.extend(event.frame.data)

    await room.connect(token["url"], token["jwt"])
    print(f"caller: in livekit room {room.name}")
    await room.local_participant.publish_track(track)
    size = int(OUT_RATE * 0.02) * 2
    for i in range(0, len(pcm), size):
        chunk = pcm[i:i + size]
        await source.capture_frame(rtc.AudioFrame(chunk, OUT_RATE, 1, len(chunk) // 2))
        while source.queued_duration > 500:
            await asyncio.sleep(0.02)
    print("caller: question sent, listening ...")
    await asyncio.sleep(seconds)
    await room.disconnect()

    from speech import pcm_to_wav
    wav_out.write_bytes(pcm_to_wav(bytes(recorded), OUT_RATE))
    heard = speech.transcribe(bytes(recorded), OUT_RATE, language) if recorded else ""
    print(f"caller: recorded {len(recorded) / 2 / OUT_RATE:.1f}s -> {wav_out}")
    print(f"caller: the bot said: {heard!r}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default=os.environ.get("MCP_BOT", "example_bot"))
    ap.add_argument("--question", default="Was ist deine Testantwort?")
    ap.add_argument("--command", default='echo "Testantwort: es funktioniert."')
    ap.add_argument("--listen", type=float, default=45.0,
                    help="seconds to wait for the answer")
    ap.add_argument("--language", default="", help="whisper language hint, e.g. de")
    ap.add_argument("--wav", default="selftest-answer.wav")
    args = ap.parse_args()

    settings = load_settings()
    cfg = next((c for c in settings.bots if c.name == args.bot), None)
    if cfg is None:
        print(f"bot '{args.bot}' not in {settings.bots}", file=sys.stderr)
        return 2
    bot = Bot(cfg, settings)

    room_file = BASE / ".selftest_room"
    room_id = room_file.read_text().strip() if room_file.exists() else ""
    if room_id and not bot.client.get_room_name(room_id):
        room_id = ""                      # gone / no access: create a fresh one
    if not room_id:
        room_id = bot.client.create_room(ROOM_NAME, invite=None)
        room_file.write_text(room_id + "\n")
    print(f"test room: {room_id}")

    call_cfg = CallConfig(enabled=True, commands=[args.command], max_seconds=300,
                          language=args.language, rooms=[room_id])
    mgr = CallManager(bot, call_cfg, on_log=lambda m: print(f"[bot] {m}"))
    if mgr.speech is None:
        print("VOICE_BASE_URL is not set", file=sys.stderr)
        return 2
    mgr.join(room_id, ROOM_NAME, TEMPLATE_EVENT)
    time.sleep(3)                       # let the bot publish + connect

    try:
        asyncio.run(run_caller(bot, room_id, args.question, args.listen,
                               Path(args.wav), args.language))
    finally:
        mgr.stop_all("selftest done")
        time.sleep(2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
