#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/silence1793/tgbot.git}"
APP_DIR="${APP_DIR:-/opt/tgbot}"
DB_DIR="${DB_DIR:-/var/lib/tgbot}"
WEBAPP_HOST="${WEBAPP_HOST:-0.0.0.0}"
WEBAPP_PORT="${WEBAPP_PORT:-8080}"
PYTHON_BIN="${PYTHON_BIN:-/root/bot/venv/bin/python}"

mkdir -p "$APP_DIR" "$DB_DIR"

if [[ ! -d "$APP_DIR/.git" ]]; then
  git clone "$REPO_URL" "$APP_DIR"
else
  git -C "$APP_DIR" fetch origin main
  git -C "$APP_DIR" reset --hard origin/main
fi

chmod +x "$APP_DIR/deploy/update_from_git.sh" "$APP_DIR/deploy/restore_server.sh"

if [[ -n "${BOT_TOKEN:-}" ]]; then
  {
    echo "BOT_TOKEN=$BOT_TOKEN"
    [[ -n "${WEBAPP_URL:-}" ]] && echo "WEBAPP_URL=$WEBAPP_URL"
    echo "WEBAPP_HOST=$WEBAPP_HOST"
    echo "WEBAPP_PORT=$WEBAPP_PORT"
    echo "DB_PATH=$DB_DIR/repairs.db"
    echo "PYTHON_BIN=$PYTHON_BIN"
  } > "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
fi

cp "$APP_DIR/deploy/tgbot.service" /etc/systemd/system/tgbot.service
cp "$APP_DIR/deploy/tgbot-autoupdate.service" /etc/systemd/system/tgbot-autoupdate.service
cp "$APP_DIR/deploy/tgbot-autoupdate.timer" /etc/systemd/system/tgbot-autoupdate.timer

systemctl daemon-reload
systemctl enable --now tgbot.service
systemctl enable --now tgbot-autoupdate.timer
