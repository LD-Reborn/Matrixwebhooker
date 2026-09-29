#!/usr/bin/env python3
"""Matrix Webhooker — a tiny HTTP server that forwards webhook alerts to Matrix.

Endpoints:
    POST /webhook   send a message to the Matrix room
        auth:   X-API-Key: <API_KEY>  (or Authorization: Bearer <API_KEY>)
        body:   {"text": "..."}
                optional: "msgtype" (m.text | m.notice), "html", "room_id"
    GET  /health    connection status (no auth)
    GET  /          usage hints (no auth)

Config: .env next to this file (see .env.example).
"""

import hmac
import os
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from flask import Flask, jsonify, request
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))
# Where runtime state (cached access token, remembered room id) lives.
# Defaults to the app directory; set MATRIX_STATE_DIR in containers so state
# survives image rebuilds (mount a volume there).
STATE_DIR = Path(os.environ.get("MATRIX_STATE_DIR", BASE_DIR))
STATE_DIR.mkdir(parents=True, exist_ok=True)
ROOM_ID_FILE = STATE_DIR / ".room_id"
TOKEN_FILE = STATE_DIR / ".token"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"ERROR: required environment variable {name} is not set (see .env.example)",
              file=sys.stderr)
        sys.exit(2)
    return value


MATRIX_HOME_SERVER = _require("MATRIX_HOME_SERVER").rstrip("/")
MATRIX_USER = _require("MATRIX_USER")
MATRIX_PASSWORD = os.environ.get("MATRIX_PASSWORD", "")
API_KEY = os.environ.get("API_KEY", "")
MATRIX_ROOM_ID = os.environ.get("MATRIX_ROOM_ID", "").strip()
MATRIX_INVITE_USER = os.environ.get("MATRIX_INVITE_USER", "").strip()
MATRIX_ROOM_NAME = os.environ.get("MATRIX_ROOM_NAME", "Webhooker Alerts")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))

if not MATRIX_PASSWORD:
    print("ERROR: MATRIX_PASSWORD is not set (see .env.example)", file=sys.stderr)
    sys.exit(2)
if not API_KEY:
    print("ERROR: API_KEY is not set (see .env.example). "
          "Generate one with:  python3 -c 'import secrets; print(secrets.token_urlsafe(24))'",
          file=sys.stderr)
    sys.exit(2)

# ---------------------------------------------------------------------------
# Minimal Matrix client-REST wrapper
# ---------------------------------------------------------------------------


class MatrixError(RuntimeError):
    def __init__(self, status: int, payload):
        self.status = status
        self.payload = payload
        detail = payload
        if isinstance(payload, dict):
            detail = payload.get("error", payload)
        super().__init__(f"HTTP {status}: {detail}")


class Matrix:
    def __init__(self, homeserver: str, user: str, password: str, timeout: float = 15.0):
        self.hs = homeserver
        self.user = user
        self.password = password
        self.timeout = timeout
        self.token: str | None = None
        self.device_id: str | None = None
        self.http = requests.Session()
        self._load_stored_token()

    def _load_stored_token(self) -> None:
        try:
            with open(TOKEN_FILE, encoding="utf-8") as fh:
                lines = [line.strip() for line in fh if line.strip()]
            if lines:
                self.token = lines[0]
                self.device_id = lines[1] if len(lines) > 1 else None
        except OSError:
            pass

    def _save_token(self) -> None:
        try:
            fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self.token + "\n")
                if self.device_id:
                    fh.write(self.device_id + "\n")
        except OSError as exc:
            print(f"WARNING: could not save token cache: {exc}", file=sys.stderr)

    def _clear_token(self) -> None:
        self.token = None
        try:
            os.remove(TOKEN_FILE)
        except OSError:
            pass

    # -- core ---------------------------------------------------------------

    def login(self, retries: int = 3) -> None:
        flows = self._plain_request("GET", f"{self.hs}/_matrix/client/v3/login")["flows"]
        if not any(f.get("type") == "m.login.password" for f in flows):
            raise MatrixError(0, "homeserver does not offer a m.login.password flow")
        body = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": self.user},
            "password": self.password,
        }
        for attempt in range(retries + 1):
            resp = self.http.post(f"{self.hs}/_matrix/client/v3/login", json=body,
                                  timeout=self.timeout)
            if resp.status_code == 429 and attempt < retries:
                # Rate-limited (M_LIMIT_EXCEEDED): honor retry_after_ms and try again.
                try:
                    wait_ms = int(resp.json().get("retry_after_ms", 5000))
                except ValueError:
                    wait_ms = 5000
                wait_s = wait_ms // 1000 + 1
                print(f"Login rate-limited, waiting {wait_s}s before retry ...",
                      file=sys.stderr)
                time.sleep(wait_s)
                continue
            if resp.status_code >= 400:
                raise MatrixError(resp.status_code, _safe_json(resp))
            data = resp.json()
            self.token = data["access_token"]
            self.device_id = data.get("device_id")
            self._save_token()
            return
        raise MatrixError(0, "could not log in (still rate-limited after retries)")

    def whoami(self) -> dict:
        return self._request("GET", f"{self.hs}/_matrix/client/v3/account/whoami")

    # -- public ---------------------------------------------------------------

    def send_message(self, room_id: str, payload: dict) -> dict:
        for attempt in (1, 2):
            txn = f"whk-{secrets.token_hex(8)}"
            url = f"{self.hs}/_matrix/client/v3/rooms/{quote(room_id)}/send/m.room.message/{txn}"
            try:
                return self._request("PUT", url, payload)
            except MatrixError as exc:
                if (
                    exc.status == 403
                    and not attempt == 2
                    and "not in the room" in str(exc.payload).lower()
                ):
                    # Perhaps we were invited meanwhile: try joining once.
                    self.join(room_id)
                    continue
                raise

    def join(self, room_id: str) -> None:
        self._request("POST", f"{self.hs}/_matrix/client/v3/rooms/{quote(room_id)}/join")

    def create_room(self, name: str, invite: str | None) -> str:
        payload: dict = {
            "name": name,
            # This Synapse (v1.161) only accepts public / private_chat /
            # trusted_private_chat; private_chat = invite-only private room.
            "preset": "private_chat",
            "visibility": "private",
            "initial": {"type": "m.room.topic", "body": "Webhooker alerts"},
        }
        if invite:
            payload["invite"] = [invite]
        return self._request("POST", f"{self.hs}/_matrix/client/v3/createRoom", payload)[
            "room_id"
        ]

    # -- internals ------------------------------------------------------------

    def _plain_request(self, method: str, url: str) -> dict:
        resp = self.http.request(method, url, timeout=self.timeout)
        if resp.status_code >= 400:
            raise MatrixError(resp.status_code, _safe_json(resp))
        return _safe_json(resp) if resp.content else {}

    def _request(self, method: str, url: str, body: dict | None = None) -> dict:
        if not self.token:
            self.login()
        headers = {"Authorization": f"Bearer {self.token}"}
        resp = self.http.request(method, url, json=body, headers=headers, timeout=self.timeout)
        if resp.status_code == 401 and self.token is not None:
            # Token rejected (e.g. after server restart / logout): log in again, retry once.
            self._clear_token()
            self.login()
            headers = {"Authorization": f"Bearer {self.token}"}
            resp = self.http.request(method, url, json=body, headers=headers,
                                     timeout=self.timeout)
        if resp.status_code >= 400:
            raise MatrixError(resp.status_code, _safe_json(resp))
        return _safe_json(resp) if resp.content else {}


def _safe_json(resp: requests.Response):
    try:
        return resp.json()
    except ValueError:
        return resp.text


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)
MATRIX = Matrix(MATRIX_HOME_SERVER, MATRIX_USER, MATRIX_PASSWORD)
CURRENT_ROOM: str = ""


def _authorized() -> bool:
    key = request.headers.get("X-API-Key", "")
    if not key:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
    return bool(key) and hmac.compare_digest(key, API_KEY)


@app.get("/")
def index():
    return jsonify({
        "service": "matrix-webhooker",
        "endpoints": {
            "POST /webhook": {
                "auth": "header X-API-Key: <API_KEY>",
                "body": {"text": "alert message",
                         "msgtype": "m.text (default) | m.notice",
                         "html": "optional HTML body",
                         "room_id": "optional override of the default room"},
            },
            "GET /health": "connection status, no auth",
        },
        "matrix": MATRIX_HOME_SERVER,
    })


@app.get("/health")
def health():
    matrix_health: dict
    try:
        who = MATRIX.whoami()
        matrix_health = {"ok": True, "user": who.get("user_id")}
    except Exception as exc:  # noqa: BLE001 - reported verbatim by /health
        matrix_health = {"ok": False, "error": str(exc)}
    status = 200 if matrix_health["ok"] else 503
    return jsonify({
        "ok": matrix_health["ok"],
        "api_key_configured": bool(API_KEY),
        "default_room": CURRENT_ROOM or None,
        "matrix": matrix_health,
    }), status


@app.post("/webhook")
def webhook():
    if not _authorized():
        return (jsonify({"error": "unauthorized: send the API key as "
                                  "X-API-Key (or Authorization: Bearer ...)"}),
                401)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "body must be a JSON object"}), 400

    text = data.get("text") or data.get("msg")
    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "missing required field 'text'"}), 400

    msgtype = data.get("msgtype") or "m.text"
    if msgtype not in ("m.text", "m.notice"):
        return jsonify({"error": "msgtype must be m.text or m.notice"}), 400

    payload: dict = {"msgtype": msgtype, "body": text}
    if data.get("html"):
        if not isinstance(data["html"], str):
            return jsonify({"error": "'html' must be a string"}), 400
        payload["format"] = "org.matrix.custom.html"
        payload["formatted_body"] = data["html"]

    room_id = data.get("room_id") or CURRENT_ROOM
    if not room_id:
        return (jsonify({"error": "no room configured: set MATRIX_ROOM_ID in .env "
                                  "(or MATRIX_INVITE_USER so one gets created)"}),
                500)

    try:
        MATRIX.send_message(room_id, payload)
    except MatrixError as exc:
        return jsonify({"error": f"matrix send failed: {exc}"}), 502
    return jsonify({"ok": True, "room": room_id})


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def resolve_room() -> str:
    if MATRIX_ROOM_ID:
        return MATRIX_ROOM_ID

    if os.path.exists(ROOM_ID_FILE):
        with open(ROOM_ID_FILE, encoding="utf-8") as fh:
            saved = fh.read().strip()
        if saved:
            print(f"Using saved room from {ROOM_ID_FILE}: {saved}")
            return saved

    if not MATRIX_INVITE_USER:
        print("ERROR: no room configured. Set MATRIX_ROOM_ID in .env, or set "
              "MATRIX_INVITE_USER so a room can be created and you can be invited.",
              file=sys.stderr)
        sys.exit(2)

    room_id = MATRIX.create_room(MATRIX_ROOM_NAME, MATRIX_INVITE_USER)
    with open(ROOM_ID_FILE, "w", encoding="utf-8") as fh:
        fh.write(room_id + "\n")
    print(f"Created room '{MATRIX_ROOM_NAME}' and invited {MATRIX_INVITE_USER}")
    print(f"Room ID: {room_id}  (saved to {ROOM_ID_FILE}, "
          "you can also pin it as MATRIX_ROOM_ID in .env)")
    return room_id


def main() -> None:
    global CURRENT_ROOM
    # whoami() verifies the token (reusing a stored one if present) and
    # falls back to a fresh login via the 401 path in _request().
    who = MATRIX.whoami()
    print(f"Connected to {MATRIX_HOME_SERVER} as {who.get('user_id')} "
          f"(device: {MATRIX.device_id or 'unknown'})")
    CURRENT_ROOM = resolve_room()
    print(f"Webhooker ready: POST /webhook -> {MATRIX_USER} -> room {CURRENT_ROOM}")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
