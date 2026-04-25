#!/usr/bin/env bash
# IES updater — one-line install (Linux).
#
#   curl -sSL https://raw.githubusercontent.com/odoobiznes/ies-releases/master/bootstrap/install_updater.sh | sudo bash
#
# What it does, idempotently:
#   1. Installs python3.12 + venv + curl/git
#   2. Creates ies-updater system user
#   3. Drops updater.py + systemd unit at /opt/ies-updater/
#   4. Writes a default config.yml subscribing to NOTHING (operator edits)
#   5. Enables + starts the systemd service
#
# After it runs, edit /opt/ies-updater/config.yml to add subscriptions, then
#   sudo systemctl restart ies-updater
#
# Tested 2026-04-25 on Ubuntu 24.04 (192.168.122.136).
set -euo pipefail

ROOT=/opt/ies-updater
USER=ies-updater
RAW="https://raw.githubusercontent.com/odoobiznes"

[[ $EUID -eq 0 ]] || { echo "ERROR: must run as root (use sudo)"; exit 1; }

log() { printf "\033[1;36m[%s]\033[0m %s\n" "$(date +%H:%M:%S)" "$*"; }

log "1/6 apt deps"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3.12 python3.12-venv python3-pip curl git ca-certificates >/dev/null

log "2/6 system user"
id -u $USER >/dev/null 2>&1 || useradd -r -s /bin/bash -d $ROOT $USER

log "3/6 directories"
mkdir -p $ROOT
chown $USER:$USER $ROOT

log "4/6 fetch updater.py + systemd unit"
# updater.py lives in iesocr-worker repo (same updater code is generic across services)
curl -sSL "$RAW/iesocr-worker/master/ies-updater/updater.py" -o $ROOT/updater.py 2>/dev/null \
    || curl -sSL "$RAW/iesocr-worker/main/ies-updater/updater.py" -o $ROOT/updater.py 2>/dev/null \
    || { echo "ERROR: cannot fetch updater.py"; exit 1; }

cat >/etc/systemd/system/ies-updater.service <<'EOF'
[Unit]
Description=IES self-update daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ies-updater
Group=ies-updater
Environment="PYTHONUNBUFFERED=1"
ExecStart=/opt/ies-updater/venv/bin/python /opt/ies-updater/updater.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

log "5/6 venv + pyyaml"
sudo -u $USER python3 -m venv $ROOT/venv
sudo -u $USER $ROOT/venv/bin/pip install --quiet --upgrade pip
sudo -u $USER $ROOT/venv/bin/pip install --quiet pyyaml

log "6/6 default config (subscribe to nothing — operator edits)"
if [[ ! -f $ROOT/config.yml ]]; then
    cat >$ROOT/config.yml <<'EOF'
# IES updater config — see https://github.com/odoobiznes/ies-releases for details.
poll_interval_sec: 900            # 15 min
http_timeout_sec: 60
keep_versions: 3                  # rollback snapshots per service
release_index: https://raw.githubusercontent.com/odoobiznes/ies-releases/master/index.json

# Optional: telemetry POSTs to this URL after every action.
# telemetry_url: https://collab.it-enterprise.pro/api/v2/update-reports
telemetry_enabled: false

# Add subscriptions below. Each install_dir is created on first install.
# Available services (see https://raw.githubusercontent.com/odoobiznes/ies-releases/master/index.json):
#   iesocr-worker   — Python OCR worker (Linux)
#   pohoda-api      — .NET Pohoda REST wrapper (Windows)
#   pohoda-digi     — .NET digitization wizard (Windows)
#   pohoda-xml-agent — .NET queue worker (Windows)
#   pohoda-kontrola  — read-only audit dashboard (Windows)
#   forms-doks       — forms wizard (Windows)
#   ies-agent-manager — service tray manager (Windows)
#
# Example (uncomment + edit):
# subscriptions:
#   iesocr-worker:
#     channel: stable
#     install_dir: /opt/iesocr-deploy

subscriptions: {}
EOF
    chown $USER:$USER $ROOT/config.yml
    chmod 0640 $ROOT/config.yml
fi

systemctl daemon-reload
systemctl enable --now ies-updater.service

log "DONE."
echo ""
echo "  Next steps:"
echo "  1. sudo nano /opt/ies-updater/config.yml      # add subscriptions"
echo "  2. sudo systemctl restart ies-updater         # apply"
echo "  3. sudo journalctl -u ies-updater -f          # watch first poll"
echo ""
systemctl status ies-updater.service --no-pager | head -8
