#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/opt/hermes-youyi-upgrade-0.19.0}"
DATA_DIR="${DATA_DIR:-/opt/hermes-youyi/data/tuoguan-data}"
SOCIAL_ROOT="${SOCIAL_ROOT:-/opt/hermes-youyi/social-research}"
RUN_USER="${RUN_USER:-hermes-youyi}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "This installer must run as root." >&2
  exit 2
fi

cat > /etc/systemd/system/hermes-youyi-social-market-research.service <<EOF
[Unit]
Description=Youyi Xiaoyou Social Market Research
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=${RUN_USER}
Group=${RUN_USER}
WorkingDirectory=${PROJECT_ROOT}
Environment=HERMES_TUOGUAN_DATA_DIR=${DATA_DIR}
Environment=HERMES_SOCIAL_RESEARCH_ROOT=${SOCIAL_ROOT}
Environment=HERMES_SOCIAL_CHROMIUM_PROFILE=${SOCIAL_ROOT}/chromium-profile
Environment=HERMES_SOCIAL_CHROMIUM=/usr/bin/chromium-browser
Environment=HERMES_SOCIAL_BROWSER_SCRIPT=${PROJECT_ROOT}/scripts/social_market_browser_fallback.js
Environment=PATH=${SOCIAL_ROOT}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
ExecStart=${PROJECT_ROOT}/.venv/bin/python ${PROJECT_ROOT}/scripts/social_market_research_runner.py --all --sleep-between --data-dir ${DATA_DIR}
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=${DATA_DIR} ${SOCIAL_ROOT} ${PROJECT_ROOT}/logs
StandardOutput=append:${PROJECT_ROOT}/logs/social-market-research.log
StandardError=append:${PROJECT_ROOT}/logs/social-market-research.log
EOF

cat > /etc/systemd/system/hermes-youyi-social-market-research.timer <<EOF
[Unit]
Description=Daily Youyi Xiaoyou Social Market Research Timer

[Timer]
OnCalendar=*-*-* 10:15:00
Persistent=true
Unit=hermes-youyi-social-market-research.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable hermes-youyi-social-market-research.timer
systemctl list-timers --all | grep hermes-youyi-social-market-research || true
echo "SOCIAL_MARKET_TIMER_INSTALLED"
