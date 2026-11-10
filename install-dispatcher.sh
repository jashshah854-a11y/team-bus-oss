#!/usr/bin/env bash
# Install or refresh the Team Bus dispatcher as a systemd service.
# Run from the checkout, as the user who owns teambus.config.json, with sudo rights:
#   ./install-dispatcher.sh
# Safe to re-run after an upgrade or a machine rebuild; it rewrites the unit
# from teambus-dispatcher.service and restarts it.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
PYTHON="$(command -v python3)"

[ -f "$DIR/teambus.config.json" ] || { echo "missing $DIR/teambus.config.json (the dispatcher's member config)"; exit 1; }
[ -f "$DIR/dispatcher.json" ] || cp "$DIR/dispatcher.example.json" "$DIR/dispatcher.json"

sed -e "s#@DIR@#$DIR#g" -e "s#@USER@#$RUN_USER#g" -e "s#@PYTHON@#$PYTHON#g" \
  "$DIR/teambus-dispatcher.service" | sudo tee /etc/systemd/system/teambus-dispatcher.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now teambus-dispatcher
sudo systemctl restart teambus-dispatcher
sleep 3
echo "teambus-dispatcher: $(systemctl is-active teambus-dispatcher)"
