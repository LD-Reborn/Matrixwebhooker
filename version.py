#!/usr/bin/env python3
"""Deployment version of the *running* code (``/status``, startup log).

Reads git once (first use, then cached for the process lifetime):
``git describe --tags`` of the checkout these modules live in — e.g.
``v1.0.0``, or ``v1.0.0-3-gab36127`` between tags, with ``-dirty``
appended when tracked files differ from HEAD.  Because the deploy rules
say *push deploy, restart right away*, the string identifies exactly the
code this process loaded — which is what makes ``/status`` able to tell
two hosts apart after a half-done rollout.  Push version tags with
``git push --follow-tags`` (and to the deploy remote) so every checkout
can name its commit; without git, a repository or a reachable tag the
answer degrades to the bare commit hash or ``unknown``.
"""
from __future__ import annotations

import functools
import subprocess
from pathlib import Path

#: the checkout this module (and therefore the running code) lives in
BASE_DIR = Path(__file__).resolve().parent


@functools.lru_cache(maxsize=1)
def app_version() -> str:
    """One-line version string of the deployed code, never raising."""
    try:
        out = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty",
             "--match", "v*"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"
