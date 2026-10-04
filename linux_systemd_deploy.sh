#!/usr/bin/env bash
# linux_systemd_deploy.sh — one-time systemd deployment of MatrixWebhooker.
#
# Run once from the app directory (it cds there itself):
#
#     sudo bash linux_systemd_deploy.sh
#
# What it does:
#   1. creates a virtualenv at ./.venv (reused if it already exists) and
#      installs requirements.txt into it;
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
VENV="$APP_DIR/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  echo "Creating virtualenv at $VENV ..."
  python3 -m venv "$VENV" \
    || fail "python3 -m venv failed — install the venv package first (e.g. apt install python3-venv)"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  echo "Installing Python dependencies (requirements.txt) ..."
  "$VENV/bin/python" -m pip install --quiet -r requirements.txt
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
Environment=PATH=$RUN_HOME/.local/bin:$RUN_HOME/.opencode/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
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
