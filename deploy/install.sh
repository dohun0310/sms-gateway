#!/usr/bin/env bash
# Install or update the SMS gateway. Run with sudo from the repository root.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
SRC=$(cd "$(dirname "$0")/.." && pwd)

id sms-gateway >/dev/null 2>&1 ||
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin sms-gateway

rm -rf /opt/sms-gateway/venv
install -d -o root -g root -m 0755 /opt/sms-gateway
/usr/bin/python3 -m venv /opt/sms-gateway/venv
BUILD=$(mktemp -d)  # build outside the repository so no root-owned files are left in it
trap 'rm -rf "$BUILD"' EXIT
cp -r "$SRC/pyproject.toml" "$SRC/sms_gateway" "$BUILD/"
/opt/sms-gateway/venv/bin/pip install --quiet --no-cache-dir "$BUILD"

install -d -o root -g sms-gateway -m 0750 /etc/sms-gateway
if [ ! -e /etc/sms-gateway/config.toml ]; then
    install -o root -g sms-gateway -m 0640 "$SRC/deploy/config.example.toml" /etc/sms-gateway/config.toml
    echo "created /etc/sms-gateway/config.toml - edit it before starting the service"
fi

install -o root -g root -m 0644 "$SRC/deploy/50-sms-gateway.rules" /etc/polkit-1/rules.d/50-sms-gateway.rules
install -o root -g root -m 0644 "$SRC/deploy/sms-gateway.service" /etc/systemd/system/sms-gateway.service
systemctl daemon-reload

if systemctl is-active --quiet sms-gateway; then
    systemctl restart sms-gateway
    echo "restarted sms-gateway"
else
    echo "installed; start with: sudo systemctl enable --now sms-gateway"
fi
