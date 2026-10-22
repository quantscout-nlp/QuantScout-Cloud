#!/usr/bin/env bash
# One-time setup of the always-on worker on a fresh Debian/Ubuntu VM (for example a
# Google Cloud e2-micro, which is in the always-free tier in us-east1, us-central1
# and us-west1). Run as root:  sudo bash setup_vm.sh
#
# Afterwards: put the desk's secrets in /etc/recovery-desk.env (template created
# below) and run  sudo systemctl enable --now recovery-desk
# To update to a newer reviewed commit later:  sudo bash setup_vm.sh  (re-runs safely)
set -euo pipefail

REPO=https://github.com/quantscout-nlp/QuantScout-Cloud.git
BRANCH=recovery-desk

apt-get update -qq
apt-get install -y -qq git python3 python3-venv unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades     # automatic OS security patches

id desk >/dev/null 2>&1 || useradd --system --home /opt/recovery-desk --shell /usr/sbin/nologin desk
install -d -o root -g root -m 755 /opt/recovery-desk
if [ -d /opt/recovery-desk/app/.git ]; then
  git -C /opt/recovery-desk/app fetch -q origin "$BRANCH"
  git -C /opt/recovery-desk/app checkout -q --detach "origin/$BRANCH"
else
  git clone -q --branch "$BRANCH" --single-branch "$REPO" /opt/recovery-desk/app
fi
chown -R root:root /opt/recovery-desk/app                  # the service user can't modify its own code
echo "Running commit: $(git -C /opt/recovery-desk/app rev-parse --short HEAD)"

python3 -m venv /opt/recovery-desk/venv
/opt/recovery-desk/venv/bin/pip install -q --upgrade pip
/opt/recovery-desk/venv/bin/pip install -q -r /opt/recovery-desk/app/requirements-worker.txt

if [ ! -f /etc/recovery-desk.env ]; then
  cat > /etc/recovery-desk.env <<'ENV'
# Recovery Desk worker secrets. Desk-only keys: do not reuse the QuantScout engine's.
ALPACA_ID=
ALPACA_SECRET=
WEBULL_APP_KEY=
WEBULL_APP_SECRET=
POLYGON_KEY=
TIINGO_KEY=
GOOGLE_OAUTH_CLIENT_ID=
GOOGLE_OAUTH_CLIENT_SECRET=
GOOGLE_OAUTH_REFRESH_TOKEN=
RECOVERY_DRIVE_FOLDER_ID=
RECOVERY_SNAPSHOT_KEY=
RECOVERY_RVOL_BASIS=SIP
ENV
fi
chown root:desk /etc/recovery-desk.env
chmod 640 /etc/recovery-desk.env

install -m 644 /opt/recovery-desk/app/deploy/recovery-desk.service /etc/systemd/system/recovery-desk.service
systemctl daemon-reload
systemctl restart recovery-desk 2>/dev/null || true
echo "Fill /etc/recovery-desk.env, then: sudo systemctl enable --now recovery-desk && journalctl -u recovery-desk -f"
