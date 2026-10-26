"""End-to-end self test for OUTGOING calls — no Element involved.

Runs both sides in one process, in the throw-away selftest room:

  * the *bot* side is the production code: ``CallManager.place_call``
    publishes our MatrixRTC membership, sends MSC4184 ``m.call.notify``
    to-device notifications, joins the SFU, and speaks the ``note`` once
    someone has picked up;
  * the *callee* side is this script: it joins the same LiveKit room,
    publishes an audio track (= "picked up"), records what the bot speaks,
    asks one question (TTS) and records + transcribes the answer.

    python3 _call_out_test.py
    python3 _call_out_test.py --note "Testanruf." --question "Wer bist du?" \
        --prompt "Antworte in einem kurzen Satz: {{transcript}}"
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

from calls import OUT_RATE, CallConfig, CallManager   # noqa: E402
from matrixbot import (Action, Bot, CALL_STATE_TYPES,  # noqa: E402
                       load_settings)
from speech import Speech, SpeechConfig, resample      # noqa: E402

ROOM_NAME = "MatrixWebhooker call selftest"


async def run_callee(bot: Bot, room_id: str, question: str, listen: float,
                     wav_out: Path, language: str = "") -> bool:
    """Join like a picking-up callee: record, ask, record again."""
    from livekit import rtc

    speech = Speech(SpeechConfig.from_env())
    # own LiveKit identity (MSC4195 /get_token path): joining via the legacy
    # /sfu/get token would reuse the *bot's* identity — LiveKit then makes the
    # two participants invisible to each other. Real clients (Element X) have
    # their own userId:deviceId, so only this fake callee needs the trick.
    token = bot.client.livekit_token(room_id, "m.call#ROOM", "mw-fake-callee")
    room = rtc.Room()
    source = rtc.AudioSource(OUT_RATE, 1)
    track = rtc.LocalAudioTrack.create_audio_track("callee", source)
    recorded = bytearray()
    bot_spoke = asyncio.Event()

    @room.on("track_subscribed")
    def _(tr, publication, participant):
        if tr.kind == rtc.TrackKind.KIND_AUDIO:
            bot_spoke.set()
            asyncio.create_task(_record(rtc, tr, recorded))

    async def _record(rtc, tr, sink: bytearray):
        stream = rtc.AudioStream(tr, sample_rate=OUT_RATE, num_channels=1)
        async for event in stream:
            sink.extend(event.frame.data)

    await room.connect(token["url"], token["jwt"])
    print(f"callee: in livekit room {room.name}")
    await room.local_participant.publish_track(track)   # this is our "pickup"
    print("callee: joined and published audio (the bot should speak now)")

    try:
        await asyncio.wait_for(bot_spoke.wait(), 30)
    except asyncio.TimeoutError:
        print("callee: FAIL - no audio track from the bot")
    await asyncio.sleep(10)                             # record note/greeting

    pcm, rate = speech.synthesize(question)
    pcm = resample(pcm, rate, OUT_RATE)
    print(f"callee: asking {len(pcm) / 2 / OUT_RATE:.1f}s of audio")
    size = int(OUT_RATE * 0.02) * 2
    for i in range(0, len(pcm), size):
        chunk = pcm[i:i + size]
        await source.capture_frame(rtc.AudioFrame(chunk, OUT_RATE, 1, len(chunk) // 2))
        while source.queued_duration > 500:
            await asyncio.sleep(0.02)
    print(f"callee: question sent, listening {listen:.0f}s ...")
    await asyncio.sleep(listen)
    await room.disconnect()

    from speech import pcm_to_wav
    wav_out.write_bytes(pcm_to_wav(bytes(recorded), OUT_RATE))
    seconds = len(recorded) / 2 / OUT_RATE
    heard = speech.transcribe(bytes(recorded), OUT_RATE, language) if recorded else ""
    print(f"callee: recorded {seconds:.1f}s -> {wav_out}")
    print(f"callee: the bot said: {heard!r}")
    return seconds > 1.0 and bool(heard.strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default=os.environ.get("MCP_BOT", "lucretia-pc"))
    ap.add_argument("--note", default="Testanruf vom MatrixWebhooker.")
    ap.add_argument("--question", default="Was ist deine Testantwort?")
    ap.add_argument("--prompt", default="Antworte in einem kurzen Satz auf die "
                                        "gehörte Frage: {{transcript}}")
    ap.add_argument("--model", default="", help="providerID/modelID ('' = server default)")
    ap.add_argument("--system", default="", help="system prompt for the action")
    ap.add_argument("--listen", type=float, default=60.0,
                    help="seconds to wait for the answer")
    ap.add_argument("--ring", type=int, default=45, help="seconds to wait for pickup")
    ap.add_argument("--language", default="", help="whisper language hint, e.g. de")
    ap.add_argument("--wav", default="selftest-out-answer.wav")
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
        room_id = ""
    if not room_id:
        room_id = bot.client.create_room(ROOM_NAME, invite=None)
        room_file.write_text(room_id + "\n")
    print(f"test room: {room_id}")

    call_cfg = CallConfig(
        enabled=True, max_seconds=300, ring_seconds=args.ring, language=args.language,
        rooms=[room_id],
        action=Action(prompt=args.prompt, system=args.system, model=args.model,
                      timeout=120))
    mgr = CallManager(bot, call_cfg,
                      on_log=lambda m: print(f"[bot] {m}", file=sys.stderr, flush=True))
    if mgr.speech is None:
        print("VOICE_BASE_URL is not set", file=sys.stderr)
        return 2
    err = mgr.place_call(room_id, ROOM_NAME, note=args.note,
                         ring_seconds=args.ring)
    if err:
        print(f"place_call failed: {err}", file=sys.stderr)
        return 1

    # the membership we published must be visible in the room state
    time.sleep(3)
    mine = [ev for ev in bot.client.room_state(room_id, CALL_STATE_TYPES)
            if (ev.get("content") or {}).get("membershipID")]
    print(f"state: {len(mine)} live call membership(s) "
          f"{[ev.get('state_key') for ev in mine]}")

    ok = False
    try:
        ok = asyncio.run(run_callee(bot, room_id, args.question, args.listen,
                                    Path(args.wav), args.language))
    finally:
        mgr.stop_all("selftest done")
        time.sleep(2)
    print("RESULT:", "OK" if ok else "FAILED (see transcript above)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
