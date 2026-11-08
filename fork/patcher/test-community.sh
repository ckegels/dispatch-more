#!/bin/bash
# The way the user's own server goes: Dispatcharr installed by the Proxmox community script
# (community-scripts/ProxmoxVE, install/dispatcharr-install.sh -- a release tarball in
# /opt/dispatcharr, uv's .venv, root services running start-*.sh, nginx serving /assets/ from
# frontend/dist), in a clean Debian 13 container with systemd as its first process. The script
# itself needs a Proxmox host, so its steps are done here one by one, as it does them.
#
#   fork/patcher/test-community.sh <older release .tar.gz> <this release .tar.gz>
#
#   1. Dispatcharr (the older release's version) the community script's way, then the one
#      command with the older release.
#   2. Default: the community script's `update` with Dispatch More on (its steps, pinned to this
#      release's version: it always takes the newest Dispatcharr), then the one command again.
#      UPGRADE=1: the one command's --upgrade instead, which does both.
#   3. The page's Uninstall: stock, judged file by file against Dispatcharr's own release tarball.
#   4. The one command, then its --uninstall: stock again.
#
# Needs Docker and the internet (apt, NodeSource, uv, GitHub). KEEP=1 leaves the container.
set -uo pipefail

OLD="$(realpath "${1:?usage: test-community.sh <older release .tar.gz> <this release .tar.gz>}")"
NEW="$(realpath "${2:?usage: test-community.sh <older release .tar.gz> <this release .tar.gz>}")"
REPO="$(git rev-parse --show-toplevel)"
NAME="dm-community-$$"
IMAGE="dispatch-more-test-systemd:13"
LOG="${LOG:-$(mktemp -t dm-community-XXXX.log)}"
PASS=0 FAIL=0

ok()  { echo "  ok    $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL  $1"; FAIL=$((FAIL + 1)); }
check() { if eval "$2" >>"$LOG" 2>&1; then ok "$1"; else bad "$1"; fi; }
inside() { docker exec "$NAME" bash -c "$1"; }
version_of() { tar -xzOf "$1" dispatch-more/manifest.json | python3 -c "import json,sys;m=json.load(sys.stdin);print(m[sys.argv[1]])" "$2"; }
OLD_V="$(version_of "$OLD" for_dispatcharr)" OLD_R="$(version_of "$OLD" release)"
NEW_V="$(version_of "$NEW" for_dispatcharr)" NEW_R="$(version_of "$NEW" release)"
echo "Log: $LOG"
echo "Proxmox community script layout: $OLD_R (Dispatcharr $OLD_V) -> Dispatcharr $NEW_V -> $NEW_R${UPGRADE:+ (--upgrade)}"

cleanup() { [ -n "${KEEP:-}" ] || docker rm -f "$NAME" >/dev/null 2>&1; }
trap cleanup EXIT

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "Making $IMAGE..."
  docker run --name "$NAME-base" debian:13 bash -c \
    "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq systemd systemd-sysv curl ca-certificates sudo git python3 >/dev/null" >>"$LOG" 2>&1 \
    && docker commit "$NAME-base" "$IMAGE" >/dev/null; docker rm -f "$NAME-base" >/dev/null 2>&1
fi
# An LXC from inside: its own cgroup namespace, no /.dockerenv
docker run -d --name "$NAME" --privileged --cgroupns=private \
  --tmpfs /run --tmpfs /run/lock "$IMAGE" /sbin/init >/dev/null
docker exec "$NAME" rm -f /.dockerenv
for _ in $(seq 1 30); do inside "systemctl is-system-running 2>/dev/null | grep -qE 'running|degraded'" && break; sleep 1; done
docker cp "$OLD" "$NAME:/root/old.tar.gz"; docker cp "$NEW" "$NAME:/root/new.tar.gz"
docker cp "$REPO/fork/patcher/quick-install.sh" "$NAME:/root/quick-install.sh"
docker cp "$REPO/fork/scripts/dispatcharr-shell.sh" "$NAME:/root/dispatcharr-shell.sh"

# Dispatcharr $1 as install/dispatcharr-install.sh puts it (its helpers -- setup_nodejs,
# setup_postgresql, setup_uv, fetch_and_deploy_gh_release -- done with the same tools)
community_install() {
  inside "set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq build-essential python3-dev libpq-dev nginx redis-server ffmpeg procps streamlink postgresql >/dev/null
curl -fsSL https://deb.nodesource.com/setup_24.x | bash - >/dev/null
apt-get install -y -qq nodejs >/dev/null
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh >/dev/null
systemctl enable -q --now postgresql redis-server
su postgres -c \"psql -qc \\\"CREATE USER dispatcharr_usr WITH PASSWORD 'dmtestpass';\\\"\"
su postgres -c \"psql -qc \\\"CREATE DATABASE dispatcharr_db OWNER dispatcharr_usr;\\\"\"
mkdir -p /opt/dispatcharr
curl -fsSL https://github.com/Dispatcharr/Dispatcharr/archive/refs/tags/v$1.tar.gz | tar --no-same-owner -xz -C /opt/dispatcharr --strip-components=1
echo $1 > /root/.dispatcharr
cd /opt/dispatcharr && uv venv --clear -q && uv sync -q && uv pip install -q uwsgi gevent celery redis daphne
install -d -m 755 /data/logos /data/recordings /data/plugins /data/db /data/uploads/m3us /data/uploads/epgs /data/m3us /data/epgs
cat > /opt/dispatcharr/.env <<'ENV'
DATABASE_URL=postgresql://dispatcharr_usr:dmtestpass@localhost:5432/dispatcharr_db
POSTGRES_DB=dispatcharr_db
POSTGRES_USER=dispatcharr_usr
POSTGRES_PASSWORD=dmtestpass
POSTGRES_HOST=localhost
CELERY_BROKER_URL=redis://localhost:6379/0
DJANGO_SECRET_KEY=community-test-secret-key-not-for-real-use-0123456789
ENV
cd /opt/dispatcharr && set -a && . ./.env && set +a && uv run python manage.py migrate --noinput >/dev/null && uv run python manage.py collectstatic --noinput >/dev/null
cd /opt/dispatcharr/frontend
node -e \"const p=require('./package.json');p.overrides=p.overrides||{};p.overrides['webworkify-webpack']='2.1.3';require('fs').writeFileSync('package.json',JSON.stringify(p,null,2));\"
rm -f package-lock.json && npm install --no-audit --progress=false --loglevel=error >/dev/null && npm run build --silent >/dev/null
cat > /etc/nginx/sites-available/dispatcharr.conf <<'NGINX'
server {
    listen 9191;
    server_name _;
    client_max_body_size 100M;
    location /assets/ { alias /opt/dispatcharr/frontend/dist/assets/; }
    location /static/ { alias /opt/dispatcharr/static/; }
    location /media/ { alias /opt/dispatcharr/media/; }
    location /ws/ {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection Upgrade;
        proxy_set_header Host \$host;
    }
    location / {
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_pass http://127.0.0.1:5656;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/dispatcharr.conf /etc/nginx/sites-enabled/dispatcharr.conf
rm -f /etc/nginx/sites-enabled/default
systemctl restart nginx
cat > /opt/dispatcharr/start-uwsgi.sh <<'SH'
#!/usr/bin/env bash
cd /opt/dispatcharr
set -a
source .env
set +a
exec .venv/bin/uwsgi --chdir=/opt/dispatcharr --module=dispatcharr.wsgi:application --master --workers=4 --gevent=400 --http=0.0.0.0:5656 --http-keepalive=1 --http-timeout=600 --socket-timeout=600 --buffer-size=65536 --post-buffering=4096 --lazy-apps --thunder-lock --die-on-term --vacuum
SH
for one in 'celery:uv run celery -A dispatcharr worker -l info -c 4 -Q celery,recordings,dvr,default' 'celerybeat:uv run celery -A dispatcharr beat -l info' 'daphne:uv run daphne -b 0.0.0.0 -p 8001 dispatcharr.asgi:application'; do
  printf '#!/usr/bin/env bash\ncd /opt/dispatcharr\nset -a\nsource .env\nset +a\nexec %s\n' \"\${one#*:}\" > /opt/dispatcharr/start-\${one%%:*}.sh
done
chmod +x /opt/dispatcharr/start-*.sh
for one in 'dispatcharr:Web Server:uwsgi:' 'dispatcharr-celery:Celery Worker:celery:Requires=dispatcharr.service' 'dispatcharr-celerybeat:Celery Beat Scheduler:celerybeat:Requires=dispatcharr.service' 'dispatcharr-daphne:WebSocket Server (Daphne):daphne:Requires=dispatcharr.service'; do
  IFS=: read -r unit what start requires <<<\"\$one\"
  printf '[Unit]\nDescription=Dispatcharr %s\nAfter=network.target postgresql.service redis-server.service\n%s\n\n[Service]\nType=simple\nWorkingDirectory=/opt/dispatcharr\nExecStart=/opt/dispatcharr/start-%s.sh\nRestart=on-failure\nRestartSec=10\nUser=root\n\n[Install]\nWantedBy=multi-user.target\n' \"\$what\" \"\$requires\" \"\$start\" > /etc/systemd/system/\$unit.service
done
systemctl daemon-reload
systemctl enable -q --now dispatcharr dispatcharr-celery dispatcharr-celerybeat dispatcharr-daphne"
}

# The community script's update_script (ct/dispatcharr.sh), its steps, pinned to $1
community_update() {
  inside "set -e
systemctl stop dispatcharr-celery dispatcharr-celerybeat dispatcharr-daphne dispatcharr
mkdir -p /root/keep && for f in .env start-celery.sh start-celerybeat.sh start-daphne.sh start-uwsgi.sh; do cp -p /opt/dispatcharr/\$f /root/keep/; done
tar -czf /opt/dispatcharr_backup_test.tar.gz -C /opt dispatcharr
find /opt/dispatcharr -mindepth 1 -maxdepth 1 -exec rm -rf {} +
curl -fsSL https://github.com/Dispatcharr/Dispatcharr/archive/refs/tags/v$1.tar.gz | tar --no-same-owner -xz -C /opt/dispatcharr --strip-components=1
echo $1 > /root/.dispatcharr
cp -p /root/keep/. /opt/dispatcharr/ -r
cd /opt/dispatcharr && rm -rf .venv && uv venv --clear -q && uv sync -q && uv pip install -q uwsgi gevent celery redis daphne
cd /opt/dispatcharr/frontend
node -e \"const p=require('./package.json');p.overrides=p.overrides||{};p.overrides['webworkify-webpack']='2.1.3';require('fs').writeFileSync('package.json',JSON.stringify(p,null,2));\"
rm -f package-lock.json && npm install --no-audit --progress=false --loglevel=error >/dev/null && npm run build --silent >/dev/null
cd /opt/dispatcharr && set -a && . ./.env && set +a && uv run python manage.py migrate --noinput >/dev/null && uv run python manage.py collectstatic --noinput >/dev/null
systemctl start dispatcharr dispatcharr-celery dispatcharr-celerybeat dispatcharr-daphne"
}

up() {
  inside 'for _ in $(seq 1 60); do bad=0; for s in dispatcharr dispatcharr-celery dispatcharr-celerybeat dispatcharr-daphne; do systemctl is-active -q $s || bad=1; done; [ $bad = 0 ] && break; sleep 2; done; [ $bad = 0 ]'
}
answers() {  # the page answers (nginx -> uWSGI) as Dispatcharr $1 with release $2 ('' = stock)
  local out
  out="$(inside "for _ in \$(seq 1 150); do curl -fsS localhost:9191/api/core/version/ 2>/dev/null | grep -q '\"version\": *\"$1\"' && break; sleep 2; done; curl -fsS localhost:9191/api/core/version/")"
  echo "$out" >>"$LOG"
  echo "$out" | grep -q "\"version\": *\"$1\"" || return 1
  if [ -n "$2" ]; then echo "$out" | grep -q "\"build\": *\"Dispatch More $2\""; else ! echo "$out" | grep -q "Dispatch More"; fi
}
# Stock: every file of Dispatcharr $1's own release tarball, byte for byte, and nothing else but
# what the community script itself makes (its venv, build, start scripts, .env, the overrides it
# writes into package.json, the lock files it lets npm and uv write)
stock_files() {
  local out
  out="$(inside "rm -rf /root/stock && mkdir -p /root/stock && curl -fsSL https://github.com/Dispatcharr/Dispatcharr/archive/refs/tags/v$1.tar.gz | tar -xz -C /root/stock --strip-components=1 && \
    diff -rq /root/stock /opt/dispatcharr \
      -x .venv -x node_modules -x dist -x static -x __pycache__ -x '*.pyc' -x media -x .env -x 'start-*.sh' \
      -x package.json -x package-lock.json -x uv.lock -x '*.egg-info' -x .ruff_cache")" || true
  echo "$out" >>"$LOG"
  [ -z "$out" ]
}
fork_units_gone() {
  inside "! ls /etc/systemd/system/ | grep -q 'dispatch-more' && ! systemctl list-units --all --no-legend 'dispatch-more*' | grep -q . && [ ! -e /etc/systemd/system/dispatcharr-celery-dvr.service ]"
}
no_tracebacks_since() {
  ! inside "journalctl --since '$1' -u 'dispatcharr*' --no-pager" | grep -q "Traceback (most recent call last)"
}
admin_token() {
  inside "bash /root/dispatcharr-shell.sh shell -c \"from apps.accounts.models import User; u=User.objects.filter(username='claude-test').first() or User.objects.create_superuser('claude-test', 'x@example.invalid', 'claude-test-pass', user_level=10); print('made')\"" >>"$LOG" 2>&1
  inside "curl -fsS -X POST -H 'Content-Type: application/json' -d '{\"username\":\"claude-test\",\"password\":\"claude-test-pass\"}' localhost:9191/api/accounts/token/" | python3 -c "import json,sys;print(json.load(sys.stdin)['access'])"
}
get() { inside "curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer $1' localhost:9191$2"; }

echo "1. Dispatcharr $OLD_V the community script's way, then the one command ($OLD_R):"
community_install "$OLD_V" >>"$LOG" 2>&1
check "stock Dispatcharr $OLD_V runs, every service up" "up && answers $OLD_V ''"
inside "DISPATCH_MORE_ARCHIVE=/root/old.tar.gz bash /root/quick-install.sh" >>"$LOG" 2>&1
check "$OLD_R is on, every service up" "up && answers $OLD_V $OLD_R"

SINCE="$(inside 'date "+%Y-%m-%d %H:%M:%S"')"
if [ -n "${UPGRADE:-}" ]; then
  echo "2. The one command's --upgrade: Dispatcharr $OLD_V -> $NEW_V and $NEW_R, in one go:"
  inside "DISPATCH_MORE_ARCHIVE=/root/new.tar.gz DISPATCH_MORE_WAIT=0 bash /root/quick-install.sh --upgrade" >>"$LOG" 2>&1
  check "...it took $OLD_R off first, on the Dispatcharr it was made for" "grep -q '$OLD_R uninstalled: stock Dispatcharr $OLD_V is back' '$LOG'"
  check "...it said it was updating the community script's install" "grep -q 'installed by community' '$LOG'"
  check "...the community script's own backup is there" "inside 'ls /opt/dispatcharr_backup_*.tar.gz'"
  check "...and where its update reads the version, $NEW_V" "inside 'grep -qx $NEW_V /root/.dispatcharr'"
else
  echo "2. The community script's update with Dispatch More on, then the one command again:"
  community_update "$NEW_V" >>"$LOG" 2>&1
  check "Dispatcharr $NEW_V runs, stock (the update replaced the folder)" "up && answers $NEW_V ''"
  inside "DISPATCH_MORE_ARCHIVE=/root/new.tar.gz bash /root/quick-install.sh" >>"$LOG" 2>&1
  # The update replaced the folder, so nothing of $OLD_R was left in it: the one command finds
  # a stock Dispatcharr and installs on it (no "updated under it" -- that is the git layout's)
  check "...the one command installed on the stock folder the update left" "grep -q 'installed over Dispatcharr $NEW_V: .* 0 removed' '$LOG' && ! grep -q 'not stock:' '$LOG'"
fi
check "$NEW_R is on Dispatcharr $NEW_V, every service up" "up && answers $NEW_V $NEW_R"
check "...the recordings worker runs" "inside 'systemctl is-active -q dispatcharr-celery-dvr'"
check "...the memory recorder is set up" "inside 'systemctl is-enabled -q dispatch-more-memory'"
TOKEN="$(admin_token)"
check "...an admin can log in" "[ -n '$TOKEN' ]"
for path in /api/core/modified-build/ /api/core/capabilities/ /api/core/app-sync/ /api/core/arrtv/ \
            /api/channels/stream-check/ /proxy/diagnostics/ /api/channels/channels/ /api/epg/grid/ /api/m3u/accounts/; do
  check "...$path answers 200" "[ \"\$(get '$TOKEN' $path)\" = 200 ]"
done
sleep 20
check "...no traceback in any Dispatcharr service's log since" "no_tracebacks_since '$SINCE'"

echo "3. The page's Uninstall:"
inside "curl -fsS -X POST -H 'Authorization: Bearer $TOKEN' localhost:9191/api/core/modified-build/uninstall/" >>"$LOG" 2>&1
inside 'for _ in $(seq 1 90); do [ -f /opt/dispatcharr/.fork-install.json ] || break; sleep 2; done' >>"$LOG" 2>&1
check "stock Dispatcharr $NEW_V is back, every service up" "up && answers $NEW_V ''"
check "...every file is Dispatcharr $NEW_V's own (its release tarball)" "stock_files $NEW_V"
check "...no unit of Dispatch More is left" "fork_units_gone"

echo "4. The one command, then its --uninstall:"
inside "DISPATCH_MORE_ARCHIVE=/root/new.tar.gz bash /root/quick-install.sh" >>"$LOG" 2>&1
check "$NEW_R is on again" "up && answers $NEW_V $NEW_R"
inside "DISPATCH_MORE_ARCHIVE=/root/new.tar.gz bash /root/quick-install.sh --uninstall" >>"$LOG" 2>&1
check "uninstalled: stock $NEW_V, every service up" "up && answers $NEW_V ''"
check "...every file is Dispatcharr $NEW_V's own" "stock_files $NEW_V"
check "...no unit of Dispatch More is left" "fork_units_gone"

echo
echo "$PASS passed, $FAIL failed (log: $LOG)"
[ "$FAIL" = 0 ]
