"""Speech I/O for Matrix calls: STT and TTS against an OpenAI-compatible gateway.

The gateway (``VOICE_BASE_URL``, e.g. ``https://ai.example.com/token/<token>``)
serves both directions:

* ``POST /v1/audio/transcriptions``  — whisper (multipart upload)
* ``POST /v1/audio/speech``          — qwen3-tts (returns 16-bit PCM wav)

One quirk of the reverse proxy in front of it: it picks the backend from the
model name found in the *JSON body or query string*, and a multipart upload
hides it — so the model is always repeated in the query string.

TTS takes natural-language style control (``VOICE_TTS_INSTRUCTION``, default
"Professionell, deutsch.") plus an ISO ``language`` code; fixed phrases are
cached on disk (``VOICE_CACHE_DIR``) so a call starts talking immediately.

Everything in and out of this module is 16-bit signed mono PCM plus a sample
rate; the wav container is only used on the wire.
"""

from __future__ import annotations

import array
import hashlib
import math
import os
import struct
import sys
import wave
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path


class SpeechError(RuntimeError):
    """Raised when the gateway rejects a request or answers unintelligibly."""


@dataclass
class SpeechConfig:
    base_url: str                       # .../token/<token>  (no trailing /v1)
    stt_model: str = "whisper-large-v3-turbo"
    tts_model: str = "qwen3-tts-customvoice"
    tts_voice: str = ""                 # empty -> the gateway's default voice
    tts_instruction: str = "Professionell, deutsch."   # qwen3-tts style control
    language: str = ""                  # e.g. "de"; empty -> whisper auto-detect

    @classmethod
    def from_env(cls) -> "SpeechConfig | None":
        base = os.environ.get("VOICE_BASE_URL", "").strip().rstrip("/")
        if not base:
            return None
        return cls(
            base_url=base,
            stt_model=os.environ.get("VOICE_STT_MODEL", cls.stt_model).strip(),
            tts_model=os.environ.get("VOICE_TTS_MODEL", cls.tts_model).strip(),
            tts_voice=os.environ.get("VOICE_TTS_VOICE", "").strip(),
            tts_instruction=os.environ.get(
                "VOICE_TTS_INSTRUCTION", cls.tts_instruction).strip(),
            language=os.environ.get("VOICE_LANGUAGE", "").strip(),
        )


# ---------------------------------------------------------------------------
# pcm <-> wav helpers
# ---------------------------------------------------------------------------


def pcm_to_wav(pcm: bytes, rate: int, channels: int = 1) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def wav_to_pcm(data: bytes) -> tuple[bytes, int]:
    """-> (16-bit mono pcm, sample rate). Stereo is downmixed."""
    try:
        with wave.open(BytesIO(data), "rb") as wf:
            rate, width, channels = wf.getframerate(), wf.getsampwidth(), wf.getnchannels()
            raw = wf.readframes(wf.getnframes())
    except (wave.Error, struct.error) as exc:
        raise SpeechError(f"gateway returned audio we cannot read: {exc}") from exc
    if width != 2:
        raise SpeechError(f"expected 16-bit pcm, got {width * 8}-bit")
    if channels > 1:
        samples = array.array("h", raw)
        mono = array.array("h", (0,) * (len(samples) // channels))
        for i in range(0, len(samples), channels):
            mono[i // channels] = sum(samples[i:i + channels]) // channels
        raw = mono.tobytes()
    return raw, rate


def resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear-interpolation resample of 16-bit mono pcm (good enough for speech)."""
    if src_rate == dst_rate or not pcm:
        return pcm
    src = array.array("h", pcm)
    n_out = int(len(src) * dst_rate / src_rate)
    out = array.array("h", (0,) * n_out)
    step = src_rate / dst_rate
    for i in range(n_out):
        pos = i * step
        j = int(pos)
        if j + 1 < len(src):
            frac = pos - j
            out[i] = int(src[j] * (1 - frac) + src[j + 1] * frac)
        else:
            out[i] = src[min(j, len(src) - 1)]
    return out.tobytes()


def tone(seconds: float, freq: float = 440.0, rate: int = 48000, amp: int = 6000) -> bytes:
    """A short sine tone — used as the 'I am listening / one moment' cue."""
    n = int(seconds * rate)
    return array.array("h", (int(amp * math.sin(2 * math.pi * freq * i / rate))
                              for i in range(n))).tobytes()


# ---------------------------------------------------------------------------
# the gateway client
# ---------------------------------------------------------------------------


class Speech:
    def __init__(self, cfg: SpeechConfig, on_log=None, timeout: float = 30.0,
                 cache_dir=None, tts_timeout: float = 90.0):
        self.cfg = cfg
        self.timeout = timeout
        #: qwen3-tts needs roughly three times the length of what it speaks, so
        #: a long answer easily runs past the ordinary 30 s budget
        self.tts_timeout = tts_timeout
        self.on_log = on_log or (lambda msg: print(f"[speech] {msg}", file=sys.stderr))
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._cache: dict[str, bytes] = {}

    def _url(self, path: str, params: dict[str, str] | None = None) -> str:
        url = f"{self.cfg.base_url}/v1/{path}"
        if params:
            from urllib.parse import urlencode
            url += "?" + urlencode(params)
        return url

    def transcribe(self, pcm: bytes, rate: int, language: str | None = None) -> str:
        """whisper: 16-bit mono pcm -> text ('' when nothing was understood)."""
        import requests      # local import: the HTTP server works without it

        lang = language if language is not None else self.cfg.language
        form: dict[str, str] = {"model": self.cfg.stt_model}
        if lang:
            form["language"] = lang
        params = {"model": self.cfg.stt_model}   # the proxy only sees it here
        try:
            resp = requests.post(
                self._url("audio/transcriptions", params),
                files={"file": ("speech.wav", pcm_to_wav(pcm, rate), "audio/wav")},
                data=form, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SpeechError(f"stt request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise SpeechError(f"stt http {resp.status_code}: {resp.text[:200]}")
        try:
            return (resp.json().get("text") or "").strip()
        except ValueError as exc:
            raise SpeechError(f"stt answer is not json: {resp.text[:200]}") from exc

    def synthesize(self, text: str, voice: str | None = None,
                   language: str | None = None) -> tuple[bytes, int]:
        """qwen3-tts: text -> (16-bit mono pcm, sample rate).

        ``instructions`` is how qwen3-tts takes natural-language style control
        ("Professionell, deutsch."); ``language`` is an ISO code and keeps the
        voice from drifting into an accent.  Both are forwarded by the gateway
        (verified: a bogus ``language`` comes back as 400, so the body does
        reach the model).
        """
        import requests

        payload = {"model": self.cfg.tts_model, "input": text,
                   "response_format": "wav"}
        v = voice if voice is not None else self.cfg.tts_voice
        if v:
            payload["voice"] = v
        if self.cfg.tts_instruction:
            payload["instructions"] = self.cfg.tts_instruction
        lang = language if language is not None else self.cfg.language
        if lang:
            payload["language"] = lang
        try:
            resp = requests.post(self._url("audio/speech"), json=payload,
                                 timeout=self.tts_timeout)
        except requests.RequestException as exc:
            raise SpeechError(f"tts request failed: {exc}") from exc
        if resp.status_code >= 400 and "language" in payload:
            # a code the tts backend does not know: speak anyway, uncontrolled
            payload.pop("language")
            try:
                resp = requests.post(self._url("audio/speech"), json=payload,
                                     timeout=self.tts_timeout)
            except requests.RequestException as exc:
                raise SpeechError(f"tts request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise SpeechError(f"tts http {resp.status_code}: {resp.text[:200]}")
        return wav_to_pcm(resp.content)

    def cached(self, text: str, rate: int = 48000,
               language: str | None = None) -> bytes:
        """Synthesize a fixed phrase once, then read it from disk.

        The greeting and the "one moment" cue must not wait for the network:
        a four-second synthesis is exactly what makes the bot greet its caller
        *after* the caller has already asked their question.
        """
        key = hashlib.sha1("|".join((
            self.cfg.tts_model, self.cfg.tts_voice, self.cfg.tts_instruction,
            (language if language is not None else self.cfg.language), text,
        )).encode()).hexdigest()
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        path = self.cache_dir / f"{key}.raw" if self.cache_dir else None
        if path is not None and path.exists():
            try:
                data = path.read_bytes()
                self._cache[key] = data
                return data
            except OSError:
                pass
        pcm, src_rate = self.synthesize(text, language=language)
        pcm = resample(pcm, src_rate, rate)
        self._cache[key] = pcm
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(pcm)
                tmp.replace(path)
            except OSError as exc:
                self.on_log(f"voice cache not writable: {exc}")
        return pcm
