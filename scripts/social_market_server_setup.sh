#!/usr/bin/env bash
set -euo pipefail

SOCIAL_ROOT="${SOCIAL_ROOT:-/opt/hermes-youyi/social-research}"
RUN_USER="${RUN_USER:-hermes-youyi}"

run_as_user() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "${RUN_USER}" -- bash -lc "$1"
  else
    su -s /bin/bash "${RUN_USER}" -c "$1"
  fi
}

if [[ "$(id -u)" -ne 0 ]]; then
  echo "This setup must run as root." >&2
  exit 2
fi

dnf install -y xorg-x11-server-Xvfb x11vnc novnc python3-websockify

install -d -o "${RUN_USER}" -g "${RUN_USER}" -m 700 "${SOCIAL_ROOT}"
install -d -o "${RUN_USER}" -g "${RUN_USER}" -m 700 "${SOCIAL_ROOT}/chromium-profile"
install -d -o "${RUN_USER}" -g "${RUN_USER}" -m 700 "${SOCIAL_ROOT}/logs"
install -d -o "${RUN_USER}" -g "${RUN_USER}" -m 755 "${SOCIAL_ROOT}/bin"

if [[ ! -f "${SOCIAL_ROOT}/package.json" ]]; then
  run_as_user "cd '${SOCIAL_ROOT}' && npm init -y >/dev/null"
fi

run_as_user "cd '${SOCIAL_ROOT}' && npm install @jackwener/opencli@1.8.6 playwright-core@1.62.1"

ln -sfn "${SOCIAL_ROOT}/node_modules/.bin/opencli" "${SOCIAL_ROOT}/bin/opencli"
chmod 700 "${SOCIAL_ROOT}/chromium-profile"

cat > "${SOCIAL_ROOT}/env.sh" <<EOF
export HERMES_SOCIAL_RESEARCH_ROOT="${SOCIAL_ROOT}"
export HERMES_SOCIAL_CHROMIUM_PROFILE="${SOCIAL_ROOT}/chromium-profile"
export HERMES_SOCIAL_CHROMIUM="/usr/bin/chromium-browser"
export HERMES_OPENCLI="${SOCIAL_ROOT}/bin/opencli"
export NODE_PATH="${SOCIAL_ROOT}/node_modules"
export PATH="${SOCIAL_ROOT}/bin:\$PATH"
EOF
chown "${RUN_USER}:${RUN_USER}" "${SOCIAL_ROOT}/env.sh"
chmod 600 "${SOCIAL_ROOT}/env.sh"

echo "SOCIAL_RESEARCH_READY ${SOCIAL_ROOT}"
echo "Next: run scripts/social_market_login_session.sh and connect through SSH tunnel."
