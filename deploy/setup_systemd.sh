#!/bin/bash
# Set up and start systemd timers.
# Run once: bash deploy/setup_systemd.sh

set -e
DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"

cp "$DEPLOY_DIR/smartmoney-hourly.service" /etc/systemd/system/
cp "$DEPLOY_DIR/smartmoney-hourly.timer"   /etc/systemd/system/
cp "$DEPLOY_DIR/smartmoney-daily.service"  /etc/systemd/system/
cp "$DEPLOY_DIR/smartmoney-daily.timer"    /etc/systemd/system/
cp "$DEPLOY_DIR/smartmoney-bot.service"    /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now smartmoney-hourly.timer
systemctl enable --now smartmoney-daily.timer
systemctl enable --now smartmoney-bot.service

echo "Timers active:"
systemctl list-timers smartmoney-*
echo "Bot service:"
systemctl status smartmoney-bot.service --no-pager | head -3
