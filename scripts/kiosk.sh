#!/usr/bin/env bash
# Launch Chromium in kiosk mode against the local backend, on HDMI (DISPLAY=:0).
# Screensaver + power management disabled so the mirror stays lit.
set -eu

URL="http://127.0.0.1:8080/"

# Best-effort: kill any prior chromium session on this display.
pkill -f "chromium.*--kiosk" 2>/dev/null || true

# Rotate HDMI display to portrait. The physical screen is mounted rotated
# so the OS still sees HDMI-1 landscape and the WM must transform it.
# Override rotation via KIOSK_ROTATE env (normal|left|right|inverted).
ROTATE="${KIOSK_ROTATE:-right}"
OUTPUT="$(xrandr --query 2>/dev/null | awk '/ connected/ {print $1; exit}')"
if [ -n "$OUTPUT" ]; then
  # --auto picks the preferred mode; without setting a mode first, rotate is
  # a silent no-op on some drivers (screen sits at a stub 320x200 vsize).
  xrandr --output "$OUTPUT" --auto --rotate "$ROTATE" 2>/dev/null || true
fi

# Kill screensaver / DPMS blanking.
xset s off        2>/dev/null || true
xset -dpms        2>/dev/null || true
xset s noblank    2>/dev/null || true
unclutter -idle 0 -root &>/dev/null &

# Pick whichever browser the RDK image ships. Firefox first because that's
# what's available on the d-robotics Ubuntu jammy image; chromium is a
# fallback for boards where it's been installed manually.
for BIN in chromium chromium-browser google-chrome firefox firefox-esr; do
  if command -v "$BIN" >/dev/null 2>&1; then
    CHROME="$BIN"
    break
  fi
done
: "${CHROME:?no browser found — install chromium-browser or firefox}"

case "$CHROME" in
  firefox*)
    # Firefox needs a dedicated profile to enable kiosk mode + skip first-run.
    PROFILE="$HOME/.mozilla/mirror-kiosk"
    mkdir -p "$PROFILE"
    # user.js — disable session restore prompts, updates, and safe-mode dialogs.
    cat > "$PROFILE/user.js" <<'EOF'
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("toolkit.startup.max_resumed_crashes", -1);
user_pref("app.update.enabled", false);
user_pref("app.update.auto", false);
user_pref("browser.aboutwelcome.enabled", false);
EOF
    exec "$CHROME" --kiosk --new-instance --profile "$PROFILE" "$URL"
    ;;
  *)
    exec "$CHROME" \
      --kiosk \
      --noerrdialogs \
      --disable-infobars \
      --disable-translate \
      --disable-features=TranslateUI \
      --no-first-run \
      --check-for-update-interval=31536000 \
      --overscroll-history-navigation=0 \
      --autoplay-policy=no-user-gesture-required \
      --start-fullscreen \
      --app="$URL"
    ;;
esac
