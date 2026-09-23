#!/usr/bin/env bash
# Install or upgrade the WebCoder hub without Docker (Debian/Ubuntu, run as root).
# The hub listens on 127.0.0.1:8787; put an HTTPS reverse proxy (e.g. an existing
# Caddy) in front of it. Re-running the script upgrades the code and keeps data.
#
#   bash deploy/install-hub-native.sh webcoder.example.com
set -euo pipefail

DOMAIN="${1:?usage: install-hub-native.sh <domain>}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/webcoder/app
VENV=/opt/webcoder/venv
DATA=/var/lib/webcoder
ENV_FILE=/etc/webcoder/hub.env
PASSWORD_FILE=/root/webcoder-admin-password.txt

if ! python3 -c 'import ensurepip, venv' 2>/dev/null; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv
fi

id webcoder >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin webcoder
install -d -o webcoder -g webcoder -m 700 "$DATA"
install -d -m 755 /opt/webcoder /etc/webcoder

# Snapshot the database first: the new code may upgrade its schema when it starts. (Once more right before it
# starts, with the old hub stopped: that one, with every last change, is what a rollback puts back.)
SNAPSHOT=""
snapshot() {
  [ -s "$DATA/hub.db" ] || return 0
  install -d -o webcoder -g webcoder -m 700 "$DATA/backups"
  SNAPSHOT="$DATA/backups/hub-$(date +%Y%m%d-%H%M%S)$1.db"
  (cd "$DATA" && runuser -u webcoder -- python3 -c \
    "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()" \
    "$DATA/hub.db" "$SNAPSHOT")
  echo "Database snapshot: $SNAPSHOT"
  ls -1t "$DATA"/backups/hub-*.db | tail -n +11 | xargs -r rm -f  # keep the 10 newest
}
snapshot ""

# Replace the code atomically; data lives in $DATA and is untouched. The previous code stays in $APP.old until
# the new hub answers: if anything below fails, it is put back and restarted.
rm -rf "$APP.new"
mkdir -p "$APP.new"
cp -r "$SRC/webcoder" "$SRC/requirements-hub.txt" "$SRC/pyproject.toml" "$APP.new/"
find "$APP.new" -name __pycache__ -type d -prune -exec rm -rf {} +
rm -rf "$APP.old"
[ -d "$APP" ] && mv "$APP" "$APP.old"
mv "$APP.new" "$APP"

UPGRADED=0
STARTED_NEW=0
put_back() {
  if [ "$UPGRADED" = 1 ] || [ ! -d "$APP.old" ]; then return; fi
  echo "The new version did not come up; putting the previous one back (database snapshot: ${SNAPSHOT:-none})" >&2
  rm -rf "$APP.failed"
  mv "$APP" "$APP.failed" && mv "$APP.old" "$APP" || return
  "$VENV/bin/pip" install --no-cache-dir -q --disable-pip-version-check -r "$APP/requirements-hub.txt" || true
  # The new version may have upgraded the database when it started: the old one gets the database it knew. (Not
  # before that: until then the old hub kept running and writing to it.)
  if [ "$STARTED_NEW" = 1 ] && [ -n "${SNAPSHOT:-}" ] && [ -f "$SNAPSHOT" ]; then
    systemctl stop webcoder-hub || true
    install -o webcoder -g webcoder -m 600 "$SNAPSHOT" "$DATA/hub.db" && rm -f "$DATA/hub.db-wal" "$DATA/hub.db-shm" &&
      echo "Database put back from $SNAPSHOT" >&2
  fi
  systemctl restart webcoder-hub || true
}
trap put_back EXIT

[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --no-cache-dir -q --disable-pip-version-check -r "$APP/requirements-hub.txt"

if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<ENV
WEBCODER_PUBLIC_URL=https://$DOMAIN
WEBCODER_DATA_DIR=$DATA
WEBCODER_HOST=127.0.0.1
WEBCODER_PORT=8787
WEBCODER_TRUSTED_PROXIES=127.0.0.1
WEBCODER_CLIENTS=chatgpt,codex
TZ=Asia/Shanghai
ENV
  chown root:webcoder "$ENV_FILE"
  chmod 640 "$ENV_FILE"
fi

# Run a command as the service user with the hub environment (extra VAR=value pairs may come first).
as_hub() { (cd "$APP" && runuser -u webcoder -- env $(grep -v '^#' "$ENV_FILE" | xargs) "$@"); }
if [ ! -s "$DATA/hub.db" ] || ! as_hub "$VENV/bin/python" -c \
    "import sqlite3,sys; sys.exit(0 if sqlite3.connect('$DATA/hub.db').execute('select 1 from users').fetchone() else 1)" 2>/dev/null; then
  PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
  as_hub WEBCODER_ADMIN_PASSWORD="$PASSWORD" "$VENV/bin/python" -m webcoder.hub init-admin --username admin >/dev/null
  umask 077
  printf '%s\n' "$PASSWORD" > "$PASSWORD_FILE"
  echo "Created panel user 'admin'; password saved to $PASSWORD_FILE"
fi

install -m 644 "$SRC/deploy/webcoder-hub.service" /etc/systemd/system/webcoder-hub.service
systemctl daemon-reload
systemctl enable --quiet webcoder-hub
systemctl stop webcoder-hub 2>/dev/null || true  # the old hub writes no more: this snapshot has all of it
snapshot "-before-start"
STARTED_NEW=1
systemctl start webcoder-hub
HEALTHY=0
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8787/healthz; then HEALTHY=1; echo; break; fi
  sleep 1
done
[ "$HEALTHY" = 1 ] || { echo "The hub did not answer on 127.0.0.1:8787 within 30 s" >&2; exit 1; }
UPGRADED=1
rm -rf "$APP.old" "$APP.failed"
echo "Hub is running on 127.0.0.1:8787. Point https://$DOMAIN at it, e.g. in /etc/caddy/Caddyfile:"
printf '\n%s {\n\tencode gzip\n\treverse_proxy 127.0.0.1:8787\n}\n' "$DOMAIN"
