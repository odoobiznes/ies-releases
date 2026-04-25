#!/usr/bin/env bash
# Install IES Fleet dashboard at port 5099. Run on any host with the updater
# already installed.
#
#   curl -sSL https://raw.githubusercontent.com/odoobiznes/ies-releases/master/demo/install_dashboard.sh | sudo bash
#
# Idempotent.
set -euo pipefail

ROOT=/opt/ies-dashboard
USER=ies-dashboard
RAW="https://raw.githubusercontent.com/odoobiznes/ies-releases/master"

[[ $EUID -eq 0 ]] || { echo "ERROR: must run as root (sudo)"; exit 1; }

echo "[1/4] system user + dirs"
id -u $USER >/dev/null 2>&1 || useradd -r -s /bin/bash -d $ROOT $USER
mkdir -p $ROOT
chown $USER:$USER $ROOT

# Allow read-only access to /opt/* installed.json files (other services owned by root or per-svc users)
# and to journalctl for ies-updater logs.
echo "[2/4] permissions: read /opt/* and journal access"
usermod -a -G systemd-journal $USER 2>/dev/null || true

echo "[3/4] fetch dashboard.py + venv + deps"
curl -sSL "$RAW/demo/dashboard.py" -o $ROOT/dashboard.py
sudo -u $USER python3 -m venv $ROOT/venv
sudo -u $USER $ROOT/venv/bin/pip install --quiet --upgrade pip
sudo -u $USER $ROOT/venv/bin/pip install --quiet 'fastapi>=0.115' 'uvicorn[standard]>=0.34' 'orjson' 'pyyaml'

echo "[4/4] systemd unit + enable"
cat >/etc/systemd/system/ies-dashboard.service <<'EOF'
[Unit]
Description=IES Fleet Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ies-dashboard
Group=ies-dashboard
Environment="PYTHONUNBUFFERED=1"
WorkingDirectory=/opt/ies-dashboard
ExecStart=/opt/ies-dashboard/venv/bin/python -m uvicorn dashboard:app --host 0.0.0.0 --port 5099
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

# Allow LAN access to :5099
ufw allow from 192.168.122.0/24 to any port 5099 comment "ies-dashboard" 2>/dev/null || true

systemctl daemon-reload
systemctl enable --now ies-dashboard.service

sleep 3
echo ""
echo "DONE."
echo "  Dashboard:  http://$(hostname -I | awk '{print $1}'):5099/"
echo "  JSON API:   http://$(hostname -I | awk '{print $1}'):5099/api/status"
echo ""
systemctl status ies-dashboard.service --no-pager | head -8
