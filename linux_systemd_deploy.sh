#!/usr/bin/env bash
# linux_systemd_deploy.sh — one-time systemd deployment of MatrixWebhooker.
#
# Run once from the app directory (it cds there itself):
#
#     sudo bash linux_systemd_deploy.sh
#
# What it does:
#   1. creates a virtualenv at ./.venv pinned to Python 3.12 (python-olm
#      ships wheels only up to cp312) and installs requirements.txt into it;
#      an existing venv is reused only if it is 3.12 with a working `import olm`,
#      otherwise it is recreated;
#   2. writes /etc/systemd/system/matrix-webhooker.service
#      (runs as the user who deployed it, WorkingDirectory = this directory,
#      the app loads .env / bots.json from there — same as `python3 server.py`);
#   3. daemon-reload + enable + start.
#
# Afterwards:
#   systemctl status matrix-webhooker          # unit status
#   journalctl -u matrix-webhooker -f          # follow logs
#   bash test_webhook.sh                        # end-to-end test
#   systemctl disable --now matrix-webhooker   # stop; remove the unit to uninstall
set -euo pipefail
cd "$(dirname "$0")"
APP_DIR="$(pwd -P)"

SERVICE=matrix-webhooker
UNIT=/etc/systemd/system/matrix-webhooker.service

fail() { echo "ERROR: $*" >&2; exit 1; }

run() {  # run a command with sudo if we're not root
  if [ -n "${SUDO:-}" ]; then sudo "$@"; else "$@"; fi
}

# --- privilege handling -----------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null \
    || fail "not root and no sudo available — re-run as root (or install sudo)"
  SUDO=1
else
  SUDO=
fi
# The service must run as the human who deploys it: it reads their .env /
# bots.json and executes their trigger commands (e.g. `opencode run`).
if [ -n "${SUDO_USER:-}" ]; then
  RUN_USER=$SUDO_USER
else
  RUN_USER=$(id -un)
fi
RUN_GROUP=$(id -gn "$RUN_USER")
RUN_HOME=$(getent passwd "$RUN_USER" | cut -d: -f6)
[ -n "$RUN_HOME" ] || fail "could not determine home directory of $RUN_USER"

# --- preflight ---------------------------------------------------------------
command -v python3 >/dev/null \
  || fail "python3 not found in PATH ($RUN_USER's PATH)"
[ -f .env ]      || fail ".env not found — copy .env.example and fill it in first"
[ -f bots.json ] || fail "bots.json not found — copy bots.example.json and fill it in first"
[ -f requirements.txt ] || fail "requirements.txt not found (script not in the app directory?)"

# --- virtualenv --------------------------------------------------------------
# Pinned to Python 3.12: requirements.txt includes python-olm, whose latest
# release (3.2.16) ships wheels only up to cp312 — on newer Pythons pip must
# build it from source and the build fails (no `import olm` -> service crash).
# Bump this only when a python-olm wheel exists for the target version.
WANT_PY_MINOR=12
VENV="$APP_DIR/.venv"

py_minor() {  # print "MAJOR.MINOR" of a python executable
  "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null
}

find_uv() {  # locate uv (sudo resets PATH, so check the user's dirs too)
  command -v uv 2>/dev/null && return 0
  local d
  for d in "$RUN_HOME/.local/bin" "$RUN_HOME/.cargo/bin"; do
    if [ -x "$d/uv" ]; then echo "$d/uv"; return 0; fi
  done
  return 1
}

venv_is_ok() {  # existing venv: right python AND the E2E stack imports
  [ -x "$VENV/bin/python" ] || return 1
  [ "$(py_minor "$VENV/bin/python")" = "3.$WANT_PY_MINOR" ] || return 1
  "$VENV/bin/python" -c 'import olm' 2>/dev/null || return 1
  return 0
}

create_venv() {
  local uv_bin
  if uv_bin=$(find_uv); then
    echo "Creating virtualenv at $VENV (Python 3.$WANT_PY_MINOR, via uv) ..."
    "$uv_bin" venv --python "3.$WANT_PY_MINOR" "$VENV"
  elif command -v "python3.$WANT_PY_MINOR" >/dev/null; then
    echo "Creating virtualenv at $VENV (Python 3.$WANT_PY_MINOR) ..."
    "python3.$WANT_PY_MINOR" -m venv "$VENV"
  else
    fail "need Python 3.$WANT_PY_MINOR for the venv (python-olm has no wheel for newer Pythons): install it, e.g. 'uv python install 3.$WANT_PY_MINOR' or 'apt install python3.$WANT_PY_MINOR', then re-run"
  fi
  echo "Installing Python dependencies (requirements.txt) ..."
  if [ -n "${uv_bin:-}" ]; then
    "$uv_bin" pip install --python "$VENV/bin/python" -q -r requirements.txt
  else
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
    "$VENV/bin/python" -m pip install --quiet -r requirements.txt
  fi
  "$VENV/bin/python" -c 'import olm' \
    || fail "dependencies installed but 'import olm' fails — python-olm incompatible with this venv"
}

if venv_is_ok; then
  echo "Reusing existing virtualenv at $VENV (Python 3.$WANT_PY_MINOR, olm OK) ..."
elif [ -e "$VENV" ]; then
  echo "Existing $VENV is not a usable Python 3.$WANT_PY_MINOR venv (wrong python or missing olm) — recreating ..."
  rm -rf "$VENV"
  create_venv
else
  create_venv
fi
# deployed via root/sudo would leave the venv root-owned — hand it to the
# service user so the running service can maintain it
if [ "$(id -u)" -eq 0 ]; then
  chown -R "$RUN_USER:$RUN_GROUP" "$VENV"
fi
PY_BIN="$VENV/bin/python"

# --- unit file ---------------------------------------------------------------
# The app loads .env itself (python-dotenv), so no EnvironmentFile is needed.
# Restart=always + enable mirrors the Docker `restart: unless-stopped` setup:
# back after crashes and reboots, until explicitly stopped.
# PATH keeps $HOME/.opencode/bin ahead of /usr/bin: the system package is an
# old opencode that lacks the flags the trigger commands use (e.g. --standalone).
UNIT_CONTENT=$(cat <<EOF
[Unit]
Description=MatrixWebhooker — webhook to Matrix bridge (multi-bot)
Documentation=file://$APP_DIR/README.md
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
UMask=0027
WorkingDirectory=$APP_DIR
Environment=HOME=$RUN_HOME
Environment=PATH=$RUN_HOME/.local/bin:$RUN_HOME/.opencode/bin:/snap/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=$PY_BIN $APP_DIR/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
)

TMP_UNIT=$(mktemp)
trap 'rm -f "$TMP_UNIT"' EXIT
printf '%s\n' "$UNIT_CONTENT" > "$TMP_UNIT"
run install -m 0644 "$TMP_UNIT" "$UNIT"

# --- register, enable, start -------------------------------------------------
# Note: `enable --now` on an ALREADY RUNNING unit is a no-op for the process
# (it keeps the old environment/config), so restart it explicitly.
run systemctl daemon-reload
run systemctl enable "$SERVICE"
if run systemctl is-active --quiet "$SERVICE"; then
  run systemctl restart "$SERVICE"
else
  run systemctl start "$SERVICE"
fi

echo
echo "✔ $SERVICE is installed and running"
echo "    unit    : $UNIT"
echo "    python  : $PY_BIN"
echo "    user    : $RUN_USER   (runs its trigger commands as this user)"
echo
echo "  systemctl status $SERVICE"
echo "  journalctl -u $SERVICE -f"
echo "  bash test_webhook.sh                 # end-to-end check"
echo
echo "  Uninstall: systemctl disable --now $SERVICE && rm -f $UNIT"

run systemctl status --no-pager "$SERVICE" || true
