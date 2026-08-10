#!/usr/bin/env bash
set -euo pipefail

SOCIAL_ROOT="${SOCIAL_ROOT:-/opt/hermes-youyi/social-research}"
RUN_USER="${RUN_USER:-hermes-youyi}"
DISPLAY_ID="${DISPLAY_ID:-92}"
VNC_PORT="${VNC_PORT:-5902}"
NOVNC_PORT="${NOVNC_PORT:-6082}"
GEOMETRY="${GEOMETRY:-1280x900x24}"
CHROMIUM="${CHROMIUM:-/usr/bin/chromium-browser}"

run_as_user() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "${RUN_USER}" -- "$@"
  else
    su -s /bin/bash "${RUN_USER}" -c "$*"
  fi
}

if [[ "$(id -u)" -ne 0 ]]; then
  echo "This login session helper must run as root." >&2
  exit 2
fi

install -d -o "${RUN_USER}" -g "${RUN_USER}" -m 700 "${SOCIAL_ROOT}/logs"
install -d -o "${RUN_USER}" -g "${RUN_USER}" -m 700 "${SOCIAL_ROOT}/chromium-profile"

pkill -f "Xvfb :${DISPLAY_ID}" 2>/dev/null || true
pkill -f "x11vnc.*${VNC_PORT}" 2>/dev/null || true
pkill -f "websockify.*${NOVNC_PORT}" 2>/dev/null || true

Xvfb ":${DISPLAY_ID}" -screen 0 "${GEOMETRY}" -nolisten tcp >"${SOCIAL_ROOT}/logs/xvfb-login.log" 2>&1 &
sleep 1
x11vnc -display ":${DISPLAY_ID}" -localhost -nopw -forever -shared -rfbport "${VNC_PORT}" >"${SOCIAL_ROOT}/logs/x11vnc-login.log" 2>&1 &
websockify --web=/usr/share/novnc "127.0.0.1:${NOVNC_PORT}" "127.0.0.1:${VNC_PORT}" >"${SOCIAL_ROOT}/logs/novnc-login.log" 2>&1 &

run_as_user env DISPLAY=":${DISPLAY_ID}" \
  "${CHROMIUM}" \
  --user-data-dir="${SOCIAL_ROOT}/chromium-profile" \
  --no-first-run \
  --no-default-browser-check \
  --disable-dev-shm-usage \
  "https://www.xiaohongshu.com" "https://www.douyin.com" "chrome://extensions" \
  >"${SOCIAL_ROOT}/logs/chromium-login.log" 2>&1 &

cat <<EOF
LOGIN_SESSION_READY
Bind: 127.0.0.1:${NOVNC_PORT}
SSH tunnel from your computer:
  ssh -L ${NOVNC_PORT}:127.0.0.1:${NOVNC_PORT} hermes-aliyun
Open:
  http://127.0.0.1:${NOVNC_PORT}/vnc.html
After login, stop this temporary session with:
  pkill -f "Xvfb :${DISPLAY_ID}" || true
  pkill -f "x11vnc.*${VNC_PORT}" || true
  pkill -f "websockify.*${NOVNC_PORT}" || true
EOF
