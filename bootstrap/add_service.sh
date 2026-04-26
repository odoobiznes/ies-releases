#!/usr/bin/env bash
# add_service.sh — interactive helper to add/update a service subscription.
#
# Usage:
#   sudo bash add_service.sh                # interactive
#   sudo bash add_service.sh <svc> <dir>    # non-interactive (defaults: channel=stable, no pin)
#   sudo bash add_service.sh <svc> <dir> stable 0.1.0    # pin to v0.1.0
#
# Reads available services from the live release index, validates,
# patches /opt/ies-updater/config.yml, and restarts ies-updater.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "ERROR: must run as root (sudo)"; exit 1; }

CFG=/opt/ies-updater/config.yml
[[ -f $CFG ]] || { echo "ERROR: $CFG not found — run install_updater.sh first"; exit 1; }

INDEX_URL=$(grep -E "^release_index:" $CFG | awk '{print $2}')
[[ -z $INDEX_URL ]] && INDEX_URL="https://raw.githubusercontent.com/odoobiznes/ies-releases/master/index.json"

# Live list of services
mapfile -t SERVICES < <(curl -fsSL "$INDEX_URL" | python3 -c "
import json, sys
d = json.load(sys.stdin)
for k in sorted(d.get('services',{}).keys()): print(k)
")

if [[ ${#SERVICES[@]} -eq 0 ]]; then
    echo "ERROR: could not fetch service list from $INDEX_URL"; exit 1
fi

SVC="${1:-}"
DIR="${2:-}"
CHANNEL="${3:-stable}"
PINNED="${4:-}"

# interactive selection
if [[ -z $SVC ]]; then
    echo "Available services in the release index:"
    printf "  - %s\n" "${SERVICES[@]}"
    echo ""
    read -rp "Service to subscribe to: " SVC
fi
if [[ ! " ${SERVICES[*]} " =~ " ${SVC} " ]]; then
    echo "ERROR: '$SVC' not in release index. Choose from: ${SERVICES[*]}"; exit 1
fi
if [[ -z $DIR ]]; then
    DEFAULT_DIR="/opt/$SVC"
    read -rp "Install dir [$DEFAULT_DIR]: " DIR
    DIR="${DIR:-$DEFAULT_DIR}"
fi

# patch config.yml — add or replace this service's subscription block
python3 - <<PY
import re, sys, pathlib
p = pathlib.Path("$CFG")
src = p.read_text()
# strip an existing block for this service if present
pat = re.compile(r"  $SVC:\s*\n(    [^\n]*\n)+", re.MULTILINE)
src = pat.sub("", src)

# add fresh block under 'subscriptions:'
block = "  $SVC:\n    channel: $CHANNEL\n    install_dir: $DIR\n"
$( [[ -n "$PINNED" ]] && printf 'block += "    pinned_version: \\\"%s\\\"\\n"\n' "$PINNED" )

if "subscriptions:" in src:
    if re.search(r"^subscriptions:\s*\{\}\s*$", src, re.MULTILINE):
        # replace empty mapping
        src = re.sub(r"^subscriptions:\s*\{\}\s*$", "subscriptions:\n" + block.rstrip(),
                     src, flags=re.MULTILINE)
    else:
        # append after the subscriptions: line
        src = re.sub(r"(^subscriptions:\s*\n)", r"\1" + block, src, count=1, flags=re.MULTILINE)
else:
    src += "\nsubscriptions:\n" + block

p.write_text(src)
print("config patched")
PY

mkdir -p "$DIR"
chown -R ies-updater:ies-updater "$DIR" 2>/dev/null || true

systemctl restart ies-updater.service
sleep 3
echo ""
systemctl status ies-updater.service --no-pager | head -7
echo ""
echo "Subscribed:  $SVC ($CHANNEL${PINNED:+, pinned $PINNED}) → $DIR"
echo "Watch:       sudo journalctl -u ies-updater -f"
