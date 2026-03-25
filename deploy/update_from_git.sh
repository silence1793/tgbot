#!/usr/bin/env bash
set -euo pipefail

cd /opt/tgbot

current_head="$(git rev-parse HEAD)"
git fetch origin main
remote_head="$(git rev-parse origin/main)"

if [[ "$current_head" == "$remote_head" ]]; then
  exit 0
fi

git reset --hard origin/main
systemctl restart tgbot.service
