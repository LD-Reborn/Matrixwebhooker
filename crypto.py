"""E2E decryption for the webhook bridge (libolm: Olm + Megolm).

Gives a bot a real Matrix *device*: an Olm account (identity + one-time
keys) uploaded via ``/keys/upload``, then:

* ingests ``to_device`` events (``m.olm`` / ``m.megolm.v1.aes-sha2``),
  establishing inbound Olm sessions and importing shared Megolm group
  session keys;
* decrypts ``m.room.encrypted`` room events (Megolm) into plaintext
  content so the watcher / history endpoints see normal ``body`` fields.

State is persisted under ``<state_dir>/crypto/`` (account pickle, inbound
Olm sessions, imported group sessions) so restarts keep working.

Only *inbound* decryption is implemented: the bot replies in plaintext
(``m.room.message``), which is displayable fine inside an encrypted room.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from pathlib import Path

import olm

OlmPreKeyMessage = olm.OlmPreKeyMessage
OlmMessage = olm.OlmMessage

OTK_COUNT = 10          # one-time curve25519 keys to keep registered


def _restore_inbound_session(pickle: bytes) -> olm.InboundSession:
    """Restore a pickled inbound Olm session.

    ``olm.InboundSession.from_pickle`` is broken in python-olm 3.2.16: its
    generic ``cls.__new__(cls)`` hits ``InboundSession.__new__``, whose
    signature requires ``(account, message)`` arguments.  Work around it by
    running the base ``Session.__new__`` (which allocates the C buffer)
    directly and unpickling into it.
    """
    from _libolm import ffi, lib  # noqa: PLC0415 - cffi handles from the binding

    obj = olm.Session.__new__(olm.InboundSession)
    pickle_buffer = ffi.new("char[]", pickle)
    ret = lib.olm_unpickle_session(
        obj._session, ffi.from_buffer(b""), 0, pickle_buffer, len(pickle))
    obj._check_error(ret)
    return obj


class CryptoError(RuntimeError):
    """Raised when a decryption step fails or no crypto context exists."""


class BotCrypto:
    """Olm/Megolm inbound crypto bound to one bot's Matrix client.

    ``client`` is anything with a duck-typed ``Matrix`` surface:
    ``hs``, ``user_id``, ``device_id`` and ``_request(method, url, body)``.
    """

    MEGOLM = "m.megolm.v1.aes-sha2"

    def __init__(self, client, state_dir: Path, on_log=None):
        self.client = client
        self.state_dir = Path(state_dir) / "crypto"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.on_log = on_log or (lambda msg: print(f"[crypto] {msg}", flush=True))
        self._lock = threading.RLock()
        self.account: olm.Account | None = None
        self._sessions: dict[str, olm.InboundSession] = {}          # sender curve25519 -> session
        self._groups: dict[tuple[str, str], olm.InboundGroupSession] = {}  # (sender_key, session_id)
        self._ready = False

    # -- lifecycle -----------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self._ready

    def _device_id(self) -> str:
        return self.client.device_id or "WEBHOOK"

    def _load_or_create(self) -> None:
        """Idempotent: loads persisted state or creates + registers the device."""
        with self._lock:
            if self._ready:
                return
            acct_f = self.state_dir / "account.b64"
            if acct_f.exists():
                try:
                    self.account = olm.Account.from_pickle(acct_f.read_bytes())
                except Exception as exc:  # noqa: BLE001
                    raise CryptoError(f"could not load saved Olm account: {exc}") from exc
            else:
                self.account = olm.Account()
                self._upload_keys()     # also persists the account
                self.on_log(f"registered crypto device {self._device_id()} "
                            f"({self.account.identity_keys['ed25519']})")
            self._load_sessions()
            self._ready = True

    @staticmethod
    def _canonical(obj: dict) -> str:
        """Canonical JSON used for key signatures on this homeserver."""
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                          sort_keys=True)

    def _sign(self, obj: dict) -> str:
        """Sign a key object (over its canonical JSON, *without* the
        signatures field) with this device's ed25519 key."""
        return self.account.sign(self._canonical(obj))

    def share_keys(self, include_device_keys: bool = True) -> dict:
        """Build a ``/keys/upload`` body: device keys and signed one-time
        keys (MSC3827 ``signed_curve25519`` form — what ``/keys/claim``
        returns to senders)."""
        assert self.account is not None
        ident = self.account.identity_keys
        device_id = self._device_id()
        user_id = self.client.user_id
        body: dict = {}

        if include_device_keys:
            # This homeserver expects *device_keys* to be a single flat
            # DeviceKeys object (with user_id inside it), and the signature
            # is over the canonical JSON of that object itself.
            device_obj = {
                "algorithms": ["m.olm.v1.curve25519-aes-sha2", self.MEGOLM],
                "device_id": device_id,
                "user_id": user_id,
                "keys": {
                    f"curve25519:{device_id}": ident["curve25519"],
                    f"ed25519:{device_id}": ident["ed25519"],
                },
            }
            device_obj["signatures"] = {
                user_id: {f"ed25519:{device_id}": self._sign(device_obj)}}
            body["device_keys"] = device_obj

        one_time_keys: dict[str, dict] = {}
        for key_id, key in (self.account.one_time_keys.get("curve25519") or {}).items():
            one_time_keys[f"signed_curve25519:{key_id}"] = {
                "key": key,
                "signatures": {
                    user_id: {f"ed25519:{device_id}":
                              self._sign({"key": key})}},
            }
        if one_time_keys:
            body["one_time_keys"] = one_time_keys
        return body

    def _save_account(self) -> None:
        """Persist the Olm account.  Must run after every successful key
        generation + upload: the account (with its one-time keys) is the
        only thing that can decrypt prekey messages built from them."""
        assert self.account is not None
        (self.state_dir / "account.b64").write_bytes(self.account.pickle())

    def _upload_keys(self) -> None:
        self.account.generate_one_time_keys(OTK_COUNT)
        body = self.share_keys(include_device_keys=True)
        self.client._request("POST", f"{self.client.hs}/_matrix/client/v3/keys/upload", body)
        self._save_account()

    def refresh_one_time_keys(self) -> None:
        """Generate fresh one-time keys and publish them (device identity
        unchanged).  Call when the server-side OTK pool runs low."""
        self.account.generate_one_time_keys(OTK_COUNT)
        self.client._request(
            "POST", f"{self.client.hs}/_matrix/client/v3/keys/upload",
            self.share_keys(include_device_keys=False))
        self._save_account()

    # -- session store -------------------------------------------------------

    def _sess_file(self) -> Path:
        return self.state_dir / "sessions.json"

    def _load_sessions(self) -> None:
        try:
            data = json.loads(self._sess_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for sender_key, b64 in data.get("sessions", {}).items():
            try:
                self._sessions[sender_key] = _restore_inbound_session(
                    base64.b64decode(b64))
            except Exception:  # noqa: BLE001 - corrupt entry: drop it
                pass
        for pair, b64 in data.get("groups", {}).items():
            try:
                sender_key, session_id = pair.split("|", 1)
                self._groups[(sender_key, session_id)] = \
                    olm.InboundGroupSession.from_pickle(base64.b64decode(b64))
            except Exception:  # noqa: BLE001
                pass
        if self._sessions or self._groups:
            self.on_log(f"restored {len(self._sessions)} olm session(s), "
                        f"{len(self._groups)} megolm group key(s)")

    def _persist_sessions(self) -> None:
        with self._lock:
            data = {
                "ts": int(time.time()),
                "sessions": {sk: base64.b64encode(sess.pickle()).decode("ascii")
                             for sk, sess in self._sessions.items()},
                "groups": {f"{sk}|{sid}": base64.b64encode(g.pickle()).decode("ascii")
                           for (sk, sid), g in self._groups.items()},
            }
            self._sess_file().write_text(
                json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")

    # -- to-device ingestion --------------------------------------------------

    def handle_todevice(self, events: list[dict]) -> None:
        """Process one /sync batch of to-device events (any type)."""
        self._load_or_create()
        with self._lock:
            for ev in events:
                try:
                    self._handle_one_todevice(ev)
                except olm.OlmSessionError as exc:
                    self.on_log(f"olm to-device rejected: {exc}")
                except (CryptoError, ValueError):
                    raise
                except Exception as exc:  # noqa: BLE001 - per-event failures
                    self.on_log(f"todevice event failed: {exc!r}")
            self._persist_sessions()

    def _sess_decrypt(self, session: olm.InboundSession, ciphertext: str) -> str:
        """Decrypt a to-device ciphertext with an established Olm session.

        libolm 3.x senders wrap *every* message in the pre-key format (even
        follow-up messages of an established session); older clients switch
        to the plain format after the first.  Try both wrappers and return
        the first that decrypts.
        """
        last_err: Exception | None = None
        for wrap in (OlmPreKeyMessage, OlmMessage):
            try:
                return session.decrypt(wrap(ciphertext))
            except olm.OlmSessionError as exc:
                last_err = exc
        raise last_err if last_err is not None else olm.OlmSessionError("no attempt")

    def _handle_one_todevice(self, ev: dict) -> None:
        etype = ev.get("type")
        content = ev.get("content") or {}
        sender_key = str(content.get("sender_key") or "").strip()

        if etype == "m.olm":
            inner = content.get("content") or {}
            message_type = int(inner.get("type", 1))
            ciphertext = str(inner.get("ciphertext") or "")
            if not sender_key or not ciphertext:
                return
            session = self._sessions.get(sender_key)
            if message_type == 0 or session is None:
                # Prekey message: (re)establish a session from it.
                try:
                    session = olm.InboundSession(
                        self.account, OlmPreKeyMessage(ciphertext))
                except olm.OlmSessionError as exc:
                    # E.g. BAD_MESSAGE_KEY_ID: not aimed at one of our one-time
                    # keys (or already consumed). Nothing to do.
                    self.on_log(f"no olm session for {sender_key[:16]}…: {exc}")
                    return
                self._sessions[sender_key] = session
            plaintext = self._sess_decrypt(session, ciphertext)
            self._plaintext_from_sender(plaintext)

        elif etype in (self.MEGOLM, "m.megolm.v2.aes-sha2"):
            ciphertext = str(content.get("ciphertext") or "")
            if not sender_key or not ciphertext:
                return
            session = self._sessions.get(sender_key)
            if session is None:
                self.on_log(
                    f"megolm key from {sender_key[:16]}… but no olm session; "
                    "ask the sender to send the message again")
                return
            plaintext = self._sess_decrypt(session, ciphertext)
            self._plaintext_from_sender(plaintext)

        elif etype == "m.add_secret_key":
            # Olm SAS / backup secret — not needed for room decryption here.
            self.on_log("ignoring m.add_secret_key (SAS/backup)")

        elif etype == "m.room_key":
            # Modern (v1.13+) key distribution: the session key arrives as a
            # *plain* to-device (not Olm-wrapped), signed by the sender user.
            self._import_plain_room_key(content, str(ev.get("sender") or ""))

        elif etype == "m.room_key_request":
            # Someone asks us for a room key.  We only decrypt (and re-share
            # nothing), so there is nothing to do beyond acknowledging.
            self.on_log("received m.room_key_request (we only decrypt; "
                        "nothing to answer)")

        else:
            # Unknown to-device type (e.g. custom bots talking to us): ignore.
            return

    # -- room-key request (sender side) --------------------------------------

    def find_device_with_curve(self, user_id: str, curve_key: str) -> str | None:
        """The device of ``user_id`` whose curve25519 identity is
        ``curve_key`` (i.e. the device whose crypto created a given Megolm
        session), or None if unknown."""
        try:
            resp = self.client._request(
                "POST", f"{self.client.hs}/_matrix/client/v3/keys/query",
                {"device_keys": {user_id: []}})
        except Exception as exc:  # noqa: BLE001 - lookup is best-effort
            self.on_log(f"keys/query for {user_id} failed: {exc}")
            return None
        devices = (resp.get("device_keys", {}) or {}).get(user_id) or {}
        for dev_id, meta in devices.items():
            if (meta.get("keys", {}) or {}).get(f"curve25519:{dev_id}") == curve_key:
                return dev_id
        return None

    def request_room_key(self, user_id: str, recipient_device: str,
                         room_id: str, sender_key: str,
                         session_id: str) -> str:
        """Ask ``user_id``'s device to re-share a Megolm session key.

        Sends a plain to-device ``m.room_key_request`` (the wire format
        matrix-nio / current Element use).  A responding client answers with
        the session key — either the modern plain ``m.room_key`` to-device or
        the legacy Olm-wrapped form; both are imported on receipt.

        ``recipient_device`` may be a specific device id or ``"*"`` (all of
        the user's devices).  Returns the request id.
        """
        self._load_or_create()
        request_id = uuid.uuid4().hex
        payload = {
            "action": "request",
            "request_id": request_id,
            "requesting_device_id": self._device_id(),
            "body": {
                "algorithm": self.MEGOLM,
                "room_id": room_id,
                "sender_key": sender_key,
                "session_id": session_id,
            },
        }
        body = {"messages": {user_id: {recipient_device or "*": payload}}}
        self.client._request(
            "PUT",
            f"{self.client.hs}/_matrix/client/v3/"
            f"sendToDevice/m.room_key_request/{request_id}",
            body)
        self.on_log(
            f"sent key request {request_id[:12]}… -> {user_id}"
            f":{recipient_device or '*'} for {sender_key[:12]}…/"
            f"{session_id[:12]}… (room {room_id})")
        return request_id

    def _plaintext_from_sender(self, plaintext: str) -> None:
        """Parse decrypted to-device plaintext; import any room keys found."""
        try:
            data = json.loads(plaintext)
        except ValueError:
            return
        for key_name in ("m.room_key", "m.forwarded_room_key"):
            rk = data.get(key_name)
            if not isinstance(rk, dict):
                continue
            if rk.get("algorithm") != self.MEGOLM:
                continue
            sender_key = rk.get("sender_key") or ""
            session_id = rk.get("session_id") or ""
            session_key = rk.get("session_key") or ""
            room_id = rk.get("room_id") or "?"
            if not (sender_key and session_id and session_key):
                continue
            if (sender_key, session_id) in self._groups:
                continue
            self._groups[(sender_key, session_id)] = \
                olm.InboundGroupSession(session_key)
            self.on_log(f"imported megolm key {session_id[:16]}… "
                        f"(from {sender_key[:16]}…) for room {room_id}")
        if "m.room_key.request" in data:
            self.on_log("received m.room_key.request (we only decrypt, not "
                        "sending encrypted — skipping)")

    def _import_plain_room_key(self, content: dict, sender_user: str) -> None:
        """Import a modern plain-format to-device room key (v1.13+).

        The content is authenticated by user signatures over its canonical
        JSON; verify when verifiable, accept with a warning when the sender
        signed nothing (we only ever import keys *about* the sender to use
        for their messages).
        """
        if content.get("algorithm") not in ("m.megolm.v1.aes-sha2",
                                            "m.megolm.v2.aes-sha2"):
            self.on_log(f"ignoring plain m.room_key (algorithm "
                        f"{content.get('algorithm')!r})")
            return
        sender_key = str(content.get("sender_key") or "")
        session_id = str(content.get("session_id") or "")
        session_key = str(content.get("session_key") or "")
        room_id = content.get("room_id") or "?"
        if not (sender_key and session_id and session_key):
            self.on_log("plain m.room_key missing sender_key/session_id/"
                        "session_key; ignoring")
            return
        if (sender_key, session_id) in self._groups:
            return
        sigs = content.get("signatures") or {}
        if sigs:
            if not self._verify_signatures(content, sigs, sender_user):
                self.on_log(
                    f"plain m.room_key from {sender_key[:16]}… failed "
                    "signature verification; dropping")
                return
        else:
            self.on_log("plain m.room_key carries no signatures (unverified "
                        "format); importing")
        self._groups[(sender_key, session_id)] = \
            olm.InboundGroupSession(session_key)
        self.on_log(f"imported megolm key {session_id[:16]}… "
                    f"(plain, from {sender_key[:16]}…) for room {room_id}")

    def _verify_signatures(self, content: dict, sigs: dict,
                           fallback_user: str) -> bool:
        """Check at least one signature over the canonical content (the field
        minus its ``signatures`` key) using the signing ed25519 key looked up
        from the sender user's device list.

        This python-olm binding's ``ed25519_verify`` returns None on success
        and raises ``OlmVerifyError`` (BAD_MESSAGE_MAC) on mismatch.
        """
        message = self._canonical({k: v for k, v in content.items()
                                   if k != "signatures"})
        for user_id, keymap in sigs.items():
            user = user_id if user_id.startswith("@") else fallback_user
            for key_id, sig in (keymap or {}).items():
                if not key_id.startswith("ed25519:"):
                    continue
                dev_id = key_id.split(":", 1)[1]
                try:
                    resp = self.client._request(
                        "POST", f"{self.client.hs}/_matrix/client/v3/keys/query",
                        {"device_keys": {user: [dev_id] if user
                                         else []}})
                except Exception:  # noqa: BLE001
                    continue
                meta = ((resp.get("device_keys", {}) or {}).get(user, {})
                        or {}).get(dev_id) or {}
                ed_key = (meta.get("keys", {}) or {}).get(f"ed25519:{dev_id}")
                if not ed_key:
                    continue
                try:
                    olm.ed25519_verify(ed_key, message, sig)
                    return True
                except Exception:  # noqa: BLE001 - bad signature / lookup
                    continue
        return False

    # -- room decryption ------------------------------------------------------

    def decrypt_room_content(self, content: dict) -> dict:
        """Decrypt a ``m.room.encrypted`` content dict -> plaintext content.

        Raises CryptoError when no group key is available.
        """
        self._load_or_create()
        sender_key = str(content.get("sender_key") or "")
        session_id = str(content.get("session_id") or "")
        ciphertext = str(content.get("ciphertext") or "")
        with self._lock:
            group = self._groups.get((sender_key, session_id))
            if group is None:
                raise CryptoError(
                    f"no megolm session for {sender_key[:16]}…/{session_id[:16]}… "
                    "(room key never shared yet)")
            plaintext, index = group.decrypt(ciphertext)
        try:
            data = json.loads(plaintext)
        except ValueError as exc:
            raise CryptoError(f"decrypted payload is not JSON: {exc}") from exc
        if not isinstance(data, dict):
            return {"body": str(data)}
        data.setdefault("_olm_index", index)
        return data

    def has_key_for(self, sender_key: str | None) -> bool:
        if not sender_key:
            return False
        with self._lock:
            return any(sk == sender_key for sk, _ in self._groups)
