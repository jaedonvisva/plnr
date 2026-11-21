#!/usr/bin/env bash
# Copy the code to the server, install dependencies and the systemd unit.
#
#   deploy/deploy.sh HOST              first install / update; does NOT start the service
#   deploy/deploy.sh HOST --env        also upload ./.env (Discord token, Gemini key)
#   deploy/deploy.sh HOST --restart    restart the service afterwards
#
# HOST is an IP or ssh alias. Users come from deploy/setup-server.sh: `plnr` owns the code and
# runs the bot (no sudo); `admin` has sudo and is used only to install/restart the service.
# Never uploads .venv, .auth (your LEARN session), .plnr (state) or .git. Override the key with
# PLNR_SSH_KEY. One passphrase prompt per user (plnr, admin): each connection is shared (ControlMaster).
set -euo pipefail

HOST="${1:?usage: deploy/deploy.sh HOST [--env] [--restart]}"; shift || true
KEY="${PLNR_SSH_KEY:-$HOME/.ssh/plnr_ed25519}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CTL="$(mktemp -d /tmp/plnr-cm.XXXXXX)"   # short path: unix sockets are limited to ~100 chars
SSH_OPTS=(-i "$KEY" -o IdentitiesOnly=yes -o ControlMaster=auto -o "ControlPath=$CTL/%C" -o ControlPersist=120)
trap 'for u in plnr admin; do ssh "${SSH_OPTS[@]}" -O exit "$u@$HOST" 2>/dev/null || true; done; rm -rf "$CTL"' EXIT
run() { ssh "${SSH_OPTS[@]}" "$@"; }

UPLOAD_ENV=0; RESTART=0
for arg in "$@"; do
  case "$arg" in --env) UPLOAD_ENV=1 ;; --restart) RESTART=1 ;; *) echo "unknown option: $arg"; exit 2 ;; esac
done

echo "==> waiting for first-boot setup (cloud-init) to finish"
run "plnr@$HOST" 'until [ -d /home/plnr/plnr ]; do sleep 2; done'
run "admin@$HOST" 'cloud-init status --wait >/dev/null 2>&1 || true'

echo "==> copying code"
rsync -az --delete -e "ssh ${SSH_OPTS[*]}" \
  --exclude '.git/' --exclude '.venv/' --exclude '.auth/' --exclude '.plnr/' --exclude '.env' \
  --exclude '__pycache__/' --exclude '.pytest_cache/' --exclude '.claude/' --exclude 'docs/' \
  "$ROOT/" "plnr@$HOST:/home/plnr/plnr/"

if [ "$UPLOAD_ENV" = 1 ]; then
  echo "==> uploading .env"
  [ -f "$ROOT/.env" ] || { echo ".env not found in $ROOT"; exit 1; }
  scp -q -i "$KEY" -o IdentitiesOnly=yes -o "ControlPath=$CTL/%C" "$ROOT/.env" "plnr@$HOST:/home/plnr/plnr/.env"
fi

echo "==> installing dependencies"
run "plnr@$HOST" 'cd /home/plnr/plnr && umask 077 && mkdir -p .plnr .auth && chmod 700 .plnr .auth \
  && ( [ -f .env ] && chmod 600 .env || true ) \
  && python3 -m venv .venv && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements.txt \
  && .venv/bin/python -c "import discord_bot" && echo "imports ok"'

echo "==> installing service"
run "admin@$HOST" 'sudo install -m 644 /home/plnr/plnr/deploy/plnr.service /etc/systemd/system/plnr.service \
  && sudo systemctl daemon-reload && sudo systemctl enable plnr >/dev/null 2>&1 && echo "service enabled"'

if [ "$RESTART" = 1 ]; then
  echo "==> restarting service"
  run "admin@$HOST" 'sudo systemctl restart plnr && sleep 3 && systemctl is-active plnr'
else
  echo "Service installed but not (re)started. After the first session push:"
  echo "  ssh -i $KEY admin@$HOST 'sudo systemctl restart plnr'   (or re-run with --restart)"
fi
