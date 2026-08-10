#!/usr/bin/env bash
set -euo pipefail

SOCIAL_ROOT="${SOCIAL_ROOT:-/opt/hermes-youyi/social-research}"
RUN_USER="${RUN_USER:-hermes-youyi}"
OPENCLI_EXTENSION_VERSION="${OPENCLI_EXTENSION_VERSION:-1.0.21}"
OPENCLI_EXTENSION_URL="${OPENCLI_EXTENSION_URL:-https://github.com/jackwener/opencli/releases/download/ext-v${OPENCLI_EXTENSION_VERSION}/opencli-extension-v${OPENCLI_EXTENSION_VERSION}.zip}"

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

dnf install -y xorg-x11-server-Xvfb x11vnc novnc python3-websockify unzip

install -d -o "${RUN_USER}" -g "${RUN_USER}" -m 700 "${SOCIAL_ROOT}"
install -d -o "${RUN_USER}" -g "${RUN_USER}" -m 700 "${SOCIAL_ROOT}/chromium-profile"
install -d -o "${RUN_USER}" -g "${RUN_USER}" -m 700 "${SOCIAL_ROOT}/logs"
install -d -o "${RUN_USER}" -g "${RUN_USER}" -m 755 "${SOCIAL_ROOT}/bin"
chown -R "${RUN_USER}:${RUN_USER}" "${SOCIAL_ROOT}/chromium-profile" "${SOCIAL_ROOT}/logs"

if [[ ! -f "${SOCIAL_ROOT}/package.json" ]]; then
  run_as_user "cd '${SOCIAL_ROOT}' && npm init -y >/dev/null"
fi

run_as_user "cd '${SOCIAL_ROOT}' && npm install @jackwener/opencli@1.8.6 playwright-core@1.62.1"

ln -sfn "${SOCIAL_ROOT}/node_modules/.bin/opencli" "${SOCIAL_ROOT}/bin/opencli"
chmod 700 "${SOCIAL_ROOT}/chromium-profile"

install -d -o "${RUN_USER}" -g "${RUN_USER}" -m 700 "${SOCIAL_ROOT}/opencli-extension"
if [[ ! -f "${SOCIAL_ROOT}/opencli-extension/manifest.json" ]]; then
  curl -L --fail --retry 3 --connect-timeout 20 \
    -o "${SOCIAL_ROOT}/opencli-extension-v${OPENCLI_EXTENSION_VERSION}.zip" \
    "${OPENCLI_EXTENSION_URL}"
  rm -rf "${SOCIAL_ROOT}/opencli-extension"/*
  unzip -q "${SOCIAL_ROOT}/opencli-extension-v${OPENCLI_EXTENSION_VERSION}.zip" -d "${SOCIAL_ROOT}/opencli-extension"
  chown -R "${RUN_USER}:${RUN_USER}" "${SOCIAL_ROOT}/opencli-extension" "${SOCIAL_ROOT}/opencli-extension-v${OPENCLI_EXTENSION_VERSION}.zip"
fi

cat > "${SOCIAL_ROOT}/env.sh" <<EOF
export HERMES_SOCIAL_RESEARCH_ROOT="${SOCIAL_ROOT}"
export HERMES_SOCIAL_CHROMIUM_PROFILE="${SOCIAL_ROOT}/chromium-profile"
export HERMES_SOCIAL_CHROMIUM="/usr/bin/chromium-browser"
export HERMES_OPENCLI="${SOCIAL_ROOT}/bin/opencli"
export HERMES_OPENCLI_EXTENSION_DIR="${SOCIAL_ROOT}/opencli-extension"
export NODE_PATH="${SOCIAL_ROOT}/node_modules"
export PATH="${SOCIAL_ROOT}/bin:\$PATH"
EOF
chown "${RUN_USER}:${RUN_USER}" "${SOCIAL_ROOT}/env.sh"
chmod 600 "${SOCIAL_ROOT}/env.sh"

echo "SOCIAL_RESEARCH_READY ${SOCIAL_ROOT}"
echo "OPENCLI_EXTENSION_READY ${SOCIAL_ROOT}/opencli-extension"
echo "Next: run scripts/social_market_login_session.sh and connect through SSH tunnel."
