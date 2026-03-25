#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/silence1793/tgbot.git}"
APP_DIR="${APP_DIR:-/opt/tgbot}"
DB_DIR="${DB_DIR:-/var/lib/tgbot}"

mkdir -p "$APP_DIR" "$DB_DIR"

if [[ ! -d "$APP_DIR/.git" ]]; then
  git clone "$REPO_URL" "$APP_DIR"
else
  git -C "$APP_DIR" fetch origin main
  git -C "$APP_DIR" reset --hard origin/main
fi

chmod +x "$APP_DIR/deploy/update_from_git.sh" "$APP_DIR/deploy/restore_server.sh"

cp "$APP_DIR/deploy/tgbot.service" /etc/systemd/system/tgbot.service
cp "$APP_DIR/deploy/tgbot-autoupdate.service" /etc/systemd/system/tgbot-autoupdate.service
cp "$APP_DIR/deploy/tgbot-autoupdate.timer" /etc/systemd/system/tgbot-autoupdate.timer

systemctl daemon-reload
systemctl enable --now tgbot.service
systemctl enable --now tgbot-autoupdate.timer
