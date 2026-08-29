#!/usr/bin/env bash
# One-shot installer for the make-up-mirror on RDK X5 (Ubuntu ARM64).
# Assumes this repo lives at /home/sunrise/make-up-mirror.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${SUDO_USER:-sunrise}"

echo "[install] repo at $REPO, user $USER_NAME"

sudo apt update
sudo apt install -y \
  python3 python3-pip python3-opencv \
  chromium-browser unclutter curl v4l-utils

# opencv from apt is fine; requirements.txt is a fallback if you prefer pip.
# sudo pip3 install -r "$REPO/backend/requirements.txt"

chmod +x "$REPO/scripts/kiosk.sh"

# Substitute the user + repo path into the unit files, then install them.
render_unit() {
  local src="$1" dst="$2"
  sed -e "s|/home/sunrise/make-up-mirror|$REPO|g" \
      -e "s|User=sunrise|User=$USER_NAME|g" \
      -e "s|/home/sunrise/.Xauthority|/home/$USER_NAME/.Xauthority|g" \
      "$src" | sudo tee "$dst" > /dev/null
}

render_unit "$REPO/systemd/makeup-mirror-backend.service" \
            /etc/systemd/system/makeup-mirror-backend.service
render_unit "$REPO/systemd/makeup-mirror-kiosk.service" \
            /etc/systemd/system/makeup-mirror-kiosk.service

sudo systemctl daemon-reload
sudo systemctl enable makeup-mirror-backend.service
sudo systemctl enable makeup-mirror-kiosk.service

# Make sure the graphical target is the default so the kiosk unit fires on boot.
sudo systemctl set-default graphical.target || true

echo
echo "[install] done. Start now with:"
echo "  sudo systemctl start makeup-mirror-backend"
echo "  sudo systemctl start makeup-mirror-kiosk"
echo "Or reboot to auto-launch."
