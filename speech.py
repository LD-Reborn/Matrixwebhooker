"""Speech I/O for Matrix calls: STT and TTS against an OpenAI-compatible gateway.

The gateway (``VOICE_BASE_URL``, e.g. ``https://ai.example.com/token/<token>``)
serves both directions:

* ``POST /v1/audio/transcriptions``  — whisper (multipart upload)
* ``POST /v1/audio/speech``          — qwen3-tts (returns 16-bit PCM wav)

One quirk of the reverse proxy in front of it: it picks the backend from the
model name found in the *JSON body or query string*, and a multipart upload
hides it — so the model is always repeated in the query string.

Everything in and out of this module is 16-bit signed mono PCM plus a sample
rate; the wav container is only used on the wire.
"""

from __future__ import annotations

import array
import math
import os
import struct
import sys
import wave
from dataclasses import dataclass
from io import BytesIO


class SpeechError(RuntimeError):
    """Raised when the gateway rejects a request or answers unintelligibly."""


@dataclass
class SpeechConfig:
    base_url: str                       # .../token/<token>  (no trailing /v1)
    stt_model: str = "whisper-large-v3-turbo"
    tts_model: str = "qwen3-tts-customvoice"
    tts_voice: str = ""                 # empty -> the gateway's default voice
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
    def __init__(self, cfg: SpeechConfig, on_log=None, timeout: float = 30.0):
        self.cfg = cfg
        self.timeout = timeout
        self.on_log = on_log or (lambda msg: print(f"[speech] {msg}", file=sys.stderr))

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

    def synthesize(self, text: str, voice: str | None = None) -> tuple[bytes, int]:
        """qwen3-tts: text -> (16-bit mono pcm, sample rate)."""
        import requests

        payload = {"model": self.cfg.tts_model, "input": text,
                   "response_format": "wav"}
        v = voice if voice is not None else self.cfg.tts_voice
        if v:
            payload["voice"] = v
        try:
            resp = requests.post(self._url("audio/speech"), json=payload,
                                 timeout=self.timeout)
        except requests.RequestException as exc:
            raise SpeechError(f"tts request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise SpeechError(f"tts http {resp.status_code}: {resp.text[:200]}")
        return wav_to_pcm(resp.content)
