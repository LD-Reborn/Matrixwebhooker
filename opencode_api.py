#!/usr/bin/env python3
"""Minimal client for the OpenCode HTTP server (``opencode serve``).

Replaces the old ``opencode run`` trigger commands: instead of spawning a
CLI per message, the watcher talks to a long-lived ``opencode serve``
instance — ``OPENCODE_SERVER_URL`` (default ``http://127.0.0.1:4096``),
Basic auth as user ``opencode`` with ``OPENCODE_SERVER_PASSWORD`` (the
password ``opencode serve`` prints when the env var is not set).

Flow per action (verified against opencode v2.1.4):

  1. ``POST /api/session``            create a fresh session (title, model
     ``{providerID, id[, variant]}`` — the variant is the room's ``/effort``,
     agent, location ``{directory}``);
  2. ``PUT /api/experimental/session/<id>/instructions/entries/<key>``
     attach the system prompt (a durable instruction entry);
  3. ``POST /api/session/<id>/prompt``  admit the user text (async);
  4. poll ``GET /api/session/<id>/message`` (newest first) until an ``idle``
     message appears — its ``outcome`` is succeeded/failed/interrupted; the
     answer is the text of the newest assistant message; permission asks
     are auto-approved in between (``GET/POST /api/session/<id>/permission``)
     so a headless session can never block on an invisible prompt;
  5. ``POST /api/session/<id>/interrupt`` stops a running turn (the room's
     bare ``stop``), ``DELETE /api/session/<id>`` cleans up.

Sessions are not disposable by default: a *succeeded* run's session stays
open as the room's live conversation — ``continue_session`` admits the next
prompt to it (the watcher does this automatically while the room's session
window is open, see ``SESSION_WINDOW_SECONDS`` in ``triggers.py``) and waits
for the answer exactly like ``run``.  Otherwise the old rules hold: a
*failed* run keeps its session for inspection in the OpenCode TUI,
``keep_session`` keeps sessions always.  The webhooker's watcher manages
session lifetimes (delete after timeout/interrupt, close via ``/new``,
``/end`` or window expiry), so it always calls with ``keep_session=True``
and deletes according to the outcome.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass

import requests

#: key of the instruction entry that carries the action's system prompt
INSTRUCTION_KEY = "matrixwebhooker"

#: how long one HTTP request to the server may block (the prompt itself is
#: admitted asynchronously, so nothing here needs long timeouts)
REQUEST_TIMEOUT = 30.0

#: pause between polls of the session's message list
POLL_SECONDS = 1.5


class OpenCodeError(RuntimeError):
    """The OpenCode server answered with an error / was unreachable."""


@dataclass
class ActionResult:
    """Outcome of one action run.

    ``outcome``: ``succeeded`` | ``failed`` | ``interrupted`` | ``timeout``
    | ``error`` (client/server problem). ``text`` is the newest assistant
    message (empty unless succeeded), ``error`` the server's error note.
    """
    text: str = ""
    outcome: str = "error"
    error: str = ""
    session_id: str = ""


def split_model(spec: str) -> tuple[str, str]:
    """``ollama/bonsai-2:2b`` -> ``('ollama', 'bonsai-2:2b')`` (like ``-m``)."""
    provider, _, model = spec.partition("/")
    if not provider or not model:
        raise ValueError(f"model must be 'providerID/modelID', got {spec!r}")
    return provider, model


class OpenCodeClient:
    """Talks to one ``opencode serve`` instance (thread-safe: requests.Session)."""

    def __init__(self, base_url: str, password: str):
        self.base = base_url.rstrip("/")
        self.http = requests.Session()
        self.http.auth = ("opencode", password)

    def _req(self, method: str, path: str, body: dict | None = None,
             params: dict | None = None) -> dict:
        try:
            resp = self.http.request(method, self.base + path, json=body,
                                     params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise OpenCodeError(f"{self.base}{path}: {exc}") from exc
        if resp.status_code >= 400:
            try:
                payload = resp.json()
                detail = payload.get("message") or payload.get("error") or payload
            except ValueError:
                detail = resp.text[:300]
            raise OpenCodeError(f"HTTP {resp.status_code} {method} {path}: {detail}")
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    # -- single steps -----------------------------------------------------------

    def default_model(self, directory: str = "") -> tuple[str, str]:
        """The server's default model for a location -> ``(providerID, id)``."""
        params = ({"location[directory]": os.path.abspath(directory)}
                  if directory else None)
        data = self._req("GET", "/api/model/default", params=params).get("data") or {}
        provider = data.get("providerID")
        mid = data.get("modelID") or data.get("id")
        if not provider or not mid:
            raise OpenCodeError(f"default model lookup returned no model: {data}")
        return str(provider), str(mid)

    def create_session(self, title: str, model: str = "", agent: str = "",
                       directory: str = "", variant: str = "") -> str:
        body: dict = {"title": title}
        provider = mid = ""
        if model:
            provider, mid = split_model(model)
        elif variant:
            # a variant needs an explicit model ref: pin the server default
            provider, mid = self.default_model(directory)
        if provider:
            ref: dict = {"providerID": provider, "id": mid}
            if variant:
                ref["variant"] = variant
            body["model"] = ref
        if agent:
            body["agent"] = agent
        if directory:
            # the API insists on an absolute location
            body["location"] = {"directory": os.path.abspath(directory)}
        data = self._req("POST", "/api/session", body).get("data") or {}
        sid = data.get("id")
        if not sid:
            raise OpenCodeError(f"session create returned no id: {data}")
        return sid

    def set_system_prompt(self, session_id: str, text: str) -> None:
        self._req("PUT", f"/api/experimental/session/{session_id}"
                        f"/instructions/entries/{INSTRUCTION_KEY}",
                  {"value": {"text": text}})

    def send_prompt(self, session_id: str, text: str) -> None:
        self._req("POST", f"/api/session/{session_id}/prompt", {"text": text})

    def steer(self, session_id: str, text: str) -> None:
        """Interject into a running turn (inbox ``steer`` delivery).

        Raises ``OpenCodeError`` if the session is gone / not running —
        unlike ``interrupt`` this caller needs to know whether it landed.
        """
        self._req("POST", f"/api/session/{session_id}/prompt",
                  {"text": text, "delivery": "steer"})

    def approve_permissions(self, session_id: str, seen: set,
                            log) -> None:
        """Auto-approve pending permission asks (headless: nobody to ask).

        A rule that resolves to ``ask`` blocks the session until a client
        replies — with no client attached that turn just hangs until the
        run times out, so every request is approved with ``once``.
        ``seen`` tracks already-answered request ids; the global config
        blanket-allow normally means this list stays empty anyway.
        """
        try:
            reqs = self._req("GET",
                             f"/api/session/{session_id}/permission").get("data") or []
        except OpenCodeError:            # session already gone
            return
        for req in reqs:
            rid = req.get("id")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            what = f"{req.get('action')} {' '.join(req.get('resources') or [])}"
            log(f"[opencode] auto-approving permission {rid}: {what[:200]}")
            try:
                self._req("POST",
                          f"/api/session/{session_id}/permission/{rid}/reply",
                          {"decision": "once"})
            except OpenCodeError as exc:  # already answered / session gone
                log(f"[opencode] permission reply failed: {exc}")

    def interrupt(self, session_id: str) -> None:
        """Best-effort: a session that already finished answers 404/no-op."""
        try:
            self._req("POST", f"/api/session/{session_id}/interrupt")
        except OpenCodeError:
            pass

    def delete_session(self, session_id: str) -> None:
        try:
            self._req("DELETE", f"/api/session/{session_id}")
        except OpenCodeError:
            pass

    def session_activity(self, session_id: str) -> str:
        """One-shot snapshot of what a running session is doing (``/status``).

        Reads the message list once and classifies the newest assistant
        turn: ``thinking`` (no output yet / last step was a finished tool
        call), ``running tool <name> — awaiting its result`` (an unfinished
        tool part), ``writing the answer`` (streaming text) or ``idle``.
        Unreachable sessions answer ``unknown`` — this must never raise.
        """
        try:
            messages = self._req(
                "GET", f"/api/session/{session_id}/message").get("data") or []
        except OpenCodeError:
            return "unknown"
        for m in messages:                       # newest first
            if m.get("type") == "idle":
                return "idle"
        assistant = next((m for m in messages
                          if m.get("type") == "assistant"), None)
        parts = [p for p in ((assistant or {}).get("content") or [])
                 if isinstance(p, dict)]
        if not parts:
            return "thinking"
        last = parts[-1]
        ptype = str(last.get("type") or "").casefold()
        if "tool" in ptype:
            done = ("result" in ptype
                    or last.get("result") is not None
                    or last.get("output") is not None
                    or str(last.get("status") or "").casefold()
                    in ("completed", "complete", "done", "success", "error"))
            if done:
                return "thinking"
            name = str(last.get("toolName") or last.get("name")
                       or last.get("tool") or "").strip()
            if len(name) > 30:
                name = name[:30] + "…"
            return f"running tool {name} — awaiting its result" if name \
                else "awaiting a tool result"
        if ptype == "text" and str(last.get("text") or "").strip():
            return "writing the answer"
        return "thinking"

    # -- the orchestrated run -----------------------------------------------------

    def run(self, *, title: str, prompt: str, system: str = "", model: str = "",
            agent: str = "", directory: str = "", variant: str = "",
            timeout: float = 300.0, cancel=None, keep_session: bool = False,
            on_session=None, log=None) -> ActionResult:
        """Create a fresh session, prompt it, wait for the answer.

        ``variant`` is the model's reasoning-effort variant (the room's
        ``/effort``); empty = the model's default. ``cancel`` is polled
        between requests — when it turns True the run is interrupted (the
        room's ``stop``). ``on_session`` receives the session id as soon as
        it exists, so a stop can interrupt it even before the first poll
        and a ``/btw`` can steer it while it runs.
        """
        log = log or (lambda m: print(m, file=sys.stderr, flush=True))
        result = ActionResult()
        try:
            result.session_id = self.create_session(title, model, agent,
                                                    directory, variant)
            if on_session:
                on_session(result.session_id)
            if system:
                self.set_system_prompt(result.session_id, system)
            self.send_prompt(result.session_id, prompt)
        except OpenCodeError as exc:
            result.outcome = "error"
            result.error = str(exc)
            return result
        except ValueError as exc:            # bad model spec etc.
            result.outcome = "error"
            result.error = str(exc)
            return result
        return self._await_turn(result, timeout, cancel, keep_session, log)

    def continue_session(self, session_id: str, *, prompt: str,
                         timeout: float = 300.0, cancel=None,
                         keep_session: bool = False, log=None) -> ActionResult:
        """Send another prompt to an *existing* session (the room's live
        conversation) and wait for the answer exactly like ``run``.

        No session is created and no system prompt attached — only the
        prompt is admitted to the kept session.  An unreachable / already
        deleted session answers as ``outcome="error"``.  Session lifetime
        follows the same rules as ``run`` (see ``_await_turn``).
        """
        log = log or (lambda m: print(m, file=sys.stderr, flush=True))
        result = ActionResult(session_id=session_id)
        try:
            self.send_prompt(session_id, prompt)
        except OpenCodeError as exc:
            result.outcome = "error"
            result.error = str(exc)
            return result
        return self._await_turn(result, timeout, cancel, keep_session, log)

    def _await_turn(self, result: ActionResult, timeout: float, cancel,
                    keep_session: bool, log) -> ActionResult:
        """Poll ``result``'s session until the turn ends; fills in the
        result's ``outcome``/``text``/``error``.

        Afterwards the session is deleted unless it *failed* (kept for
        inspection in the TUI) or ``keep_session`` was set — the caller
        takes over lifetime management then (see the module docstring).
        """
        try:
            seen_perms: set = set()
            deadline = time.monotonic() + timeout
            while True:
                if cancel is not None and cancel():
                    self.interrupt(result.session_id)
                    result.outcome = "interrupted"
                    return result
                if time.monotonic() > deadline:
                    log(f"[opencode] no answer after {timeout:.0f}s: interrupting "
                        f"{result.session_id}")
                    self.interrupt(result.session_id)
                    result.outcome = "timeout"
                    return result
                time.sleep(POLL_SECONDS)
                self.approve_permissions(result.session_id, seen_perms, log)
                messages = self._req(
                    "GET", f"/api/session/{result.session_id}/message").get("data") or []
                idle = next((m for m in messages if m.get("type") == "idle"), None)
                if idle is None:
                    continue
                result.outcome = idle.get("outcome") or "failed"
                error = idle.get("error")
                if isinstance(error, dict):
                    result.error = str(error.get("message") or error)
                elif error:
                    result.error = str(error)
                if result.outcome == "succeeded":
                    # messages come newest first: the first assistant message
                    # is the final answer of this turn
                    for m in messages:
                        if m.get("type") != "assistant":
                            continue
                        parts = [c.get("text", "") for c in m.get("content") or []
                                 if c.get("type") == "text"]
                        result.text = "\n".join(p for p in parts if p).strip()
                        break
                return result
        except OpenCodeError as exc:
            result.outcome = "error"
            result.error = str(exc)
            return result
        finally:
            if result.session_id and not keep_session and result.outcome != "failed":
                self.delete_session(result.session_id)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


if __name__ == "__main__":  # tiny manual smoke test: python3 opencode_api.py "text"
    import os
    client = OpenCodeClient(os.environ.get("OPENCODE_SERVER_URL", "http://127.0.0.1:4096"),
                            os.environ.get("OPENCODE_SERVER_PASSWORD", ""))
    text = sys.argv[1] if len(sys.argv) > 1 else "Antworte genau mit einem Wort: PONG"
    r = client.run(title="opencode_api smoke test", prompt=text,
                   directory=os.getcwd(), timeout=120, log=_log)
    print(f"outcome={r.outcome} session={r.session_id}")
    if r.error:
        print(f"error: {r.error}")
    print(r.text)
