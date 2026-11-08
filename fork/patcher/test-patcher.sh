#!/bin/bash
# Tests the patcher on real stock copies of Dispatcharr: install, install again, refusing the
# wrong version and changed files, upgrading, uninstalling back to byte-for-byte stock, and
# the Docker start script. Needs a built release:
#
#   fork/patcher/test-patcher.sh fork/patcher/out/dispatch-more-v99-dispatcharr-<version>.tar.gz
set -uo pipefail

ARCHIVE="$(realpath "${1:?usage: test-patcher.sh <release .tar.gz>}")"
BASE="${BASE:-$(cat "$(dirname "$0")/BASE")}"
REPO="$(git rev-parse --show-toplevel)"
T="$(mktemp -d)"
PASS=0 FAIL=0
trap 'git -C "$REPO" worktree remove --force "$T/stock" >/dev/null 2>&1; rm -rf "$T"' EXIT
export ALLOW_NOT_ROOT=1

ok()   { echo "  ok    $1"; PASS=$((PASS + 1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL + 1)); }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

git -C "$REPO" worktree add --detach "$T/stock" "$BASE" >/dev/null 2>&1
# A stock installation has a built frontend; stand one in
mkdir -p "$T/stock/frontend/dist/assets" && echo "stock page" > "$T/stock/frontend/dist/index.html"
rm -f "$T/stock/.git"
fresh() { rm -rf "$T/app"; cp -a "$T/stock" "$T/app"; }
same_as_stock() { diff -r -q "$T/stock" "$T/app" >/dev/null; }
unpack() { rm -rf "$1"; mkdir -p "$1"; tar -xzf "$ARCHIVE" -C "$1" --strip-components=1; }

unpack "$T/pkg"
SLUG="$(python3 -c "import json;print(json.load(open('$T/pkg/manifest.json'))['slug'])")"
RELEASE="$(python3 -c "import json;print(json.load(open('$T/pkg/manifest.json'))['release'])")"
VERSION="$(python3 -c "import json;print(json.load(open('$T/pkg/manifest.json'))['for_dispatcharr'])")"

echo "Linux (systemd layout, without touching systemd):"
fresh
export STATE="$T/state"
out="$(bash "$T/pkg/install.sh" --app "$T/app" --no-systemd 2>&1)"; code=$?
check "installs" "[ $code = 0 ]"
check "the page knows it (record)" "[ -f '$T/app/.fork-install.json' ]"
check "the release is stamped in the version" "grep -q '__build__ = \".* $RELEASE\"' '$T/app/version.py'"
check "the frontend is the release's" "! grep -q 'stock page' '$T/app/frontend/dist/index.html'"
check "every file is the release's" "python3 - <<PY
import hashlib, json, os, sys
m = json.load(open('$T/pkg/manifest.json'))
for f in m['files']:
    p = os.path.join('$T/app', f['path'])
    if f['action'] == 'remove':
        assert not os.path.exists(p), p
    else:
        assert hashlib.sha256(open(p, 'rb').read()).hexdigest() == f['sha256'], p
PY"
out="$(bash "$T/pkg/install.sh" --app "$T/app" --no-systemd 2>&1)"
check "installing again changes nothing" "echo \"\$out\" | grep -q 'already installed'"
out="$(bash "$STATE/uninstall.sh" --app "$T/app" --no-systemd 2>&1)"; code=$?
check "uninstalls" "[ $code = 0 ]"
check "stock is back, byte for byte" "same_as_stock"
check "the state keeps no backup" "[ ! -d '$STATE/backup' ]"

echo "Refusing:"
fresh; rm -rf "$STATE"
sed -i "s/^__version__ = .*/__version__ = '9.9.9'/" "$T/app/version.py"
out="$(bash "$T/pkg/install.sh" --app "$T/app" --no-systemd 2>&1)"; code=$?
check "another Dispatcharr version is refused" "[ $code = 3 ] && echo \"\$out\" | grep -q 'is for Dispatcharr'"
check "...and nothing changed" "[ ! -f '$T/app/.fork-install.json' ]"
fresh; rm -rf "$STATE"
echo "# a local change" >> "$T/app/apps/proxy/live_proxy/views.py"
out="$(bash "$T/pkg/install.sh" --app "$T/app" --no-systemd 2>&1)"; code=$?
check "a file changed by something else is refused" "[ $code = 4 ] && echo \"\$out\" | grep -q 'not stock: apps/proxy/live_proxy/views.py'"
check "...and nothing changed" "[ ! -f '$T/app/.fork-install.json' ] && ! grep -q __build__ '$T/app/version.py'"
out="$(bash "$T/pkg/install.sh" --app "$T/app" --no-systemd --force 2>&1)"; code=$?
check "--force goes ahead" "[ $code = 0 ]"
bash "$STATE/uninstall.sh" --app "$T/app" --no-systemd >/dev/null 2>&1
check "...and uninstalling gives back the changed file too" "grep -q '# a local change' '$T/app/apps/proxy/live_proxy/views.py'"

echo "Upgrading:"
fresh; rm -rf "$STATE"
bash "$T/pkg/install.sh" --app "$T/app" --no-systemd >/dev/null 2>&1
unpack "$T/next"
python3 - "$T/next/manifest.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1])); m["release"] = "v-next"; json.dump(m, open(sys.argv[1], "w"))
PY
out="$(bash "$T/next/install.sh" --app "$T/app" --no-systemd 2>&1)"; code=$?
check "a newer release takes the older off first" "[ $code = 0 ] && echo \"\$out\" | grep -q 'taking it off first'"
check "...and is the one on" "grep -q '\"release\": \"v-next\"' '$T/app/.fork-install.json'"
bash "$STATE/uninstall.sh" --app "$T/app" --no-systemd >/dev/null 2>&1
check "uninstalling it gives stock, not the older release" "same_as_stock"

echo "A file changed after installing:"
fresh; rm -rf "$STATE"
bash "$T/pkg/install.sh" --app "$T/app" --no-systemd >/dev/null 2>&1
ADDED="$(python3 -c "import json;print(next(f['path'] for f in json.load(open('$T/pkg/manifest.json'))['files'] if f['action']=='add'))")"
echo "# mine" >> "$T/app/$ADDED"; echo "# mine" >> "$T/app/apps/proxy/live_proxy/views.py"
out="$(bash "$STATE/uninstall.sh" --app "$T/app" --no-systemd 2>&1)"; code=$?
check "uninstalling leaves both as they are, and says so" \
  "[ $code = 0 ] && grep -q '# mine' '$T/app/$ADDED' && grep -q '# mine' '$T/app/apps/proxy/live_proxy/views.py' && echo \"\$out\" | grep -q 'something else has changed them'"
check "...and everything else is stock" \
  "[ \"\$(diff -r -q '$T/stock' '$T/app' | wc -l)\" = 2 ]"

echo "Python's compiled copies (Dispatcharr ran the release's modules):"
fresh; rm -rf "$STATE"
bash "$T/pkg/install.sh" --app "$T/app" --no-systemd >/dev/null 2>&1
python3 - <<PY
import json, py_compile
for f in json.load(open('$T/pkg/manifest.json'))['files']:
    if f['action'] != 'remove' and f['path'].endswith('.py'):
        py_compile.compile('$T/app/' + f['path'])
PY
out="$(bash "$STATE/uninstall.sh" --app "$T/app" --no-systemd 2>&1)"; code=$?
# The release's folders (apps/channels/captions, ...) go with their __pycache__; stock's own
# modules keep theirs, which are Dispatcharr's to keep
check "uninstalling takes the release's compiled modules and their folders away" \
  "[ $code = 0 ] && [ -z \"\$(diff -r -q -x __pycache__ '$T/stock' '$T/app')\" ] && [ ! -e '$T/app/apps/channels/captions' ]"
check "...and leaves the compiled copies of stock's own modules" \
  "ls '$T/app/apps/proxy/live_proxy/__pycache__/' | grep -q '^views\.'"

echo "No built frontend before:"
fresh; rm -rf "$STATE" "$T/app/frontend/dist"
bash "$T/pkg/install.sh" --app "$T/app" --no-systemd >/dev/null 2>&1
bash "$STATE/uninstall.sh" --app "$T/app" --no-systemd >/dev/null 2>&1
check "uninstalling takes the release's frontend away again" "[ ! -e '$T/app/frontend/dist' ]"

echo "Collectstatic's copies of the frontend (static/, which Docker's nginx serves /assets/ from):"
fresh; rm -rf "$STATE"; mkdir -p "$T/app/static/admin"
cp -a "$T/app/frontend/dist/." "$T/app/static/"; echo "django admin" > "$T/app/static/admin/base.css"
bash "$T/pkg/install.sh" --app "$T/app" --no-systemd >/dev/null 2>&1
# A start collects the release's frontend; then the uninstall puts stock's back with its old times
cp -a "$T/app/frontend/dist/." "$T/app/static/"
OURS="$(cd "$T/app/frontend/dist" && find . -type f ! -name index.html | head -1)"
echo "not the release's" > "$T/app/static/assets/someone-elses.js"
bash "$STATE/uninstall.sh" --app "$T/app" --no-systemd >/dev/null 2>&1
check "uninstalling puts stock's page back in static/ too" "cmp -s '$T/stock/frontend/dist/index.html' '$T/app/static/index.html'"
check "...takes the release's assets out of it" "[ -n '$OURS' ] && [ ! -e '$T/app/static/$OURS' ]"
check "...and leaves what is not the release's" "[ -f '$T/app/static/admin/base.css' ] && [ -f '$T/app/static/assets/someone-elses.js' ]"
rm -rf "$T/app/static"
check "...everything else is stock" "same_as_stock"

echo "Docker (the start script):"
fresh; unset STATE
DATA="$T/data/$SLUG"; unpack "$DATA"
start() { DISPATCHARR_APP="$T/app" DISPATCHARR_ENTRYPOINT=/bin/echo bash "$DATA/docker-entrypoint.sh" started 2>&1; }
out="$(start)"
check "the first start installs, then starts Dispatcharr" "echo \"\$out\" | grep -q 'installed over' && echo \"\$out\" | tail -1 | grep -q started"
check "...with the layout said as Docker" "grep -q '\"layout\": \"docker\"' '$T/app/.fork-install.json'"
out="$(start)"
check "a restart leaves it as it is" "echo \"\$out\" | grep -q 'already installed'"
mkdir -p "$DATA/requests" && echo '{}' > "$DATA/requests/uninstall"
out="$(start)"
check "after the page's Uninstall, the next start is stock" "same_as_stock && [ -f '$DATA/uninstalled' ]"
out="$(start)"
check "...and stays stock" "same_as_stock && echo \"\$out\" | grep -q 'uninstalled, starting stock'"
fresh; rm -f "$DATA/uninstalled"
sed -i "s/^__version__ = .*/__version__ = '9.9.9'/" "$T/app/version.py"
# Not looking on GitHub here (that is test-docker.sh's): the release in /data as it is
out="$(DISPATCH_MORE_AUTO_UPDATE=0 start)"
check "an image of another Dispatcharr starts as stock, and says why" "echo \"\$out\" | grep -q 'not applied' && echo \"\$out\" | tail -1 | grep -q started"
# A newer image: the release in /data is for the Dispatcharr before; the one for this image is
# fetched (here from the local archive, as GitHub would hand it) and put on at the same start
fresh; rm -rf "$DATA"; unpack "$DATA"
sed -i 's/"for_dispatcharr": *"[^"]*"/"for_dispatcharr": "0.0.1"/' "$DATA/manifest.json"
out="$(DISPATCH_MORE_ARCHIVE="$ARCHIVE" start)"
check "a newer image gets the release made for it by itself, at the same start" \
  "echo \"\$out\" | grep -q 'looking for the one made for it' && echo \"\$out\" | grep -q 'installed over' && grep -q '\"for_dispatcharr\": \"$VERSION\"' '$DATA/manifest.json'"
mkdir -p "$DATA/requests" && echo '{}' > "$DATA/requests/uninstall"; start >/dev/null
sed -i 's/"for_dispatcharr": *"[^"]*"/"for_dispatcharr": "0.0.1"/' "$DATA/manifest.json"
out="$(DISPATCH_MORE_ARCHIVE="$ARCHIVE" start)"
check "...but never after the page's Uninstall" "same_as_stock && ! echo \"\$out\" | grep -q 'looking for'"

echo "One command (quick-install.sh, from a local archive rather than GitHub):"
fresh
QUICK_STATE="$T/quick-state"
out="$(DISPATCH_MORE_ARCHIVE="$ARCHIVE" DISPATCHARR_APP="$T/app" STATE="$QUICK_STATE" \
  DISPATCH_MORE_INSTALL_ARGS="--no-systemd" bash "$REPO/fork/patcher/quick-install.sh" 2>&1)"; code=$?
check "installs in one command" "[ $code = 0 ] && [ -f '$T/app/.fork-install.json' ]"
STATE="$QUICK_STATE" bash "$QUICK_STATE/uninstall.sh" --app "$T/app" --no-systemd >/dev/null 2>&1
check "and uninstalls back to stock" "same_as_stock"
quick() { DISPATCH_MORE_ARCHIVE="$ARCHIVE" DISPATCHARR_APP="$T/app" STATE="$QUICK_STATE" DISPATCH_MORE_LAYOUT=systemd \
  DISPATCH_MORE_INSTALL_ARGS="--no-systemd" DISPATCH_MORE_WAIT=0 bash "$REPO/fork/patcher/quick-install.sh" "$@" 2>&1; }
fresh; rm -rf "$QUICK_STATE"
out="$(quick --upgrade)"; code=$?
check "--upgrade on the Dispatcharr the release is made for: installs, nothing else" \
  "[ $code = 0 ] && echo \"\$out\" | grep -q 'already, the version the newest' && [ -f '$T/app/.fork-install.json' ]"
STATE="$QUICK_STATE" bash "$QUICK_STATE/uninstall.sh" --app "$T/app" --no-systemd >/dev/null 2>&1
# A release saved under a name of its own says which Dispatcharr it is for in its manifest
fresh; rm -rf "$QUICK_STATE"; cp "$ARCHIVE" "$T/release.tar.gz"
out="$(DISPATCH_MORE_ARCHIVE="$T/release.tar.gz" DISPATCHARR_APP="$T/app" STATE="$QUICK_STATE" DISPATCH_MORE_LAYOUT=systemd \
  DISPATCH_MORE_INSTALL_ARGS="--no-systemd" DISPATCH_MORE_WAIT=0 bash "$REPO/fork/patcher/quick-install.sh" --upgrade 2>&1)"; code=$?
check "...also from a release saved under any name (its manifest says which Dispatcharr)" \
  "[ $code = 0 ] && echo \"\$out\" | grep -q 'already, the version the newest' && [ -f '$T/app/.fork-install.json' ]"
STATE="$QUICK_STATE" bash "$QUICK_STATE/uninstall.sh" --app "$T/app" --no-systemd >/dev/null 2>&1
fresh; sed -i "s/^__version__ = .*/__version__ = '99.0.0'/" "$T/app/version.py"; cp "$T/app/version.py" "$T/newer-version.py"
out="$(quick --upgrade)"; code=$?
check "--upgrade on a newer Dispatcharr: nothing changed, never taken back" \
  "[ $code = 3 ] && echo \"\$out\" | grep -q 'newer than any' && [ ! -f '$T/app/.fork-install.json' ] && cmp -s '$T/app/version.py' '$T/newer-version.py'"
fresh; sed -i "s/^__version__ = .*/__version__ = '0.0.1'/" "$T/app/version.py"; cp "$T/app/version.py" "$T/older-version.py"
out="$(quick --upgrade)"; code=$?
check "--upgrade on an older Dispatcharr it did not install (or not as root): nothing changed" \
  "[ $code != 0 ] && echo \"\$out\" | grep -q 'Nothing was changed' && [ ! -f '$T/app/.fork-install.json' ] && cmp -s '$T/app/version.py' '$T/older-version.py'"

# Dispatcharr updated under an installed release, as debian_install.sh updates it: `git reset
# --hard` and a pull of the new version (every tracked file becomes the new version's, files the
# old one had and the new one has not go, untracked files -- the release's added ones, its record
# -- stay), then a new frontend build. $1: the app, $2: the old version's tree, $3: the new one's.
dispatcharr_update() {
  (cd "$2" && find . -type f -not -path './frontend/dist/*' -not -name .git) | while read -r f; do
    [ -e "$3/$f" ] || rm -f "$1/$f"
  done
  (cd "$3" && tar -cf - --exclude=./frontend/dist --exclude=./.git .) | (cd "$1" && tar -xf -)
  rm -rf "$1/frontend/dist"; cp -a "$3/frontend/dist" "$1/frontend/dist"
}
stock_tree() {  # $1: version, $2: where -- a stock copy as a server has it (built frontend)
  git -C "$REPO" worktree add --detach "$2" "v$1" >/dev/null 2>&1
  mkdir -p "$2/frontend/dist/assets" && echo "stock page $1" > "$2/frontend/dist/index.html"
  rm -f "$2/.git"
}
VERSION_OF() { python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['for_dispatcharr'])" "$1"; }
THIS="$(VERSION_OF "$T/pkg/manifest.json")"

# U: this release on its Dispatcharr, Dispatcharr updated to UPDATED_TO under it, then taken off
# with the one command. Needs the newer version's tag (git fetch upstream --tags).
UPDATED_TO="${UPDATED_TO:-}"
if [ -n "$UPDATED_TO" ] && git -C "$REPO" rev-parse -q --verify "v$UPDATED_TO^{commit}" >/dev/null; then
  echo "Dispatcharr updated from $THIS to $UPDATED_TO under this release, then uninstalled:"
  stock_tree "$UPDATED_TO" "$T/newer"
  fresh; U_STATE="$T/u-state"; rm -rf "$U_STATE"
  STATE="$U_STATE" bash "$T/pkg/install.sh" --app "$T/app" --no-systemd >/dev/null 2>&1
  dispatcharr_update "$T/app" "$T/stock" "$T/newer"
  check "...the update leaves the release's added files and record behind (what debian_install.sh does)" \
    "[ -f '$T/app/.fork-install.json' ] && ! diff -r -q '$T/newer' '$T/app' >/dev/null"
  out="$(DISPATCH_MORE_ARCHIVE="$ARCHIVE" DISPATCHARR_APP="$T/app" STATE="$U_STATE" \
    DISPATCH_MORE_INSTALL_ARGS="--no-systemd" bash "$REPO/fork/patcher/quick-install.sh" --uninstall 2>&1)"; code=$?
  check "the one command uninstalls" "[ $code = 0 ] && echo \"\$out\" | grep -q 'updated from $THIS to $UPDATED_TO'"
  check "...and leaves stock $UPDATED_TO byte for byte, not $THIS's files over it" "diff -r -q '$T/newer' '$T/app' >/dev/null"
  rm -rf "$T/newer"; git -C "$REPO" worktree prune
else
  echo "(Dispatcharr updated under the release: skipped, set UPDATED_TO to a newer Dispatcharr version)"
fi

# P: an older release on an older Dispatcharr (PREVIOUS_ARCHIVE), Dispatcharr updated to this
# release's version under it, then this release installed over it and uninstalled again.
PREVIOUS_ARCHIVE="${PREVIOUS_ARCHIVE:-}"
if [ -n "$PREVIOUS_ARCHIVE" ] && [ -f "$PREVIOUS_ARCHIVE" ]; then
  rm -rf "$T/prev"; mkdir -p "$T/prev"; tar -xzf "$PREVIOUS_ARCHIVE" -C "$T/prev" --strip-components=1
  OLD="$(VERSION_OF "$T/prev/manifest.json")"; OLDREL="$(python3 -c "import json;print(json.load(open('$T/prev/manifest.json'))['release'])")"
  echo "$OLDREL on Dispatcharr $OLD, Dispatcharr updated to $THIS, then $RELEASE:"
  stock_tree "$OLD" "$T/older"
  rm -rf "$T/app"; cp -a "$T/older" "$T/app"; P_STATE="$T/p-state"; rm -rf "$P_STATE"
  STATE="$P_STATE" bash "$T/prev/install.sh" --app "$T/app" --no-systemd >/dev/null 2>&1
  check "$OLDREL installs on $OLD" "grep -q '\"release\": \"$OLDREL\"' '$T/app/.fork-install.json'"
  dispatcharr_update "$T/app" "$T/older" "$T/stock"
  out="$(STATE="$P_STATE" bash "$T/pkg/install.sh" --app "$T/app" --no-systemd 2>&1)"; code=$?
  check "$RELEASE installs over what the update left" "[ $code = 0 ] && echo \"\$out\" | grep -q 'updated from $OLD to $THIS'"
  check "...every file is $RELEASE's" "python3 - <<PY
import hashlib, json, os
m = json.load(open('$T/pkg/manifest.json'))
for f in m['files']:
    p = os.path.join('$T/app', f['path'])
    if f['action'] == 'remove':
        assert not os.path.exists(p), p
    else:
        assert hashlib.sha256(open(p, 'rb').read()).hexdigest() == f['sha256'], p
PY"
  out="$(STATE="$P_STATE" bash "$P_STATE/uninstall.sh" --app "$T/app" --no-systemd 2>&1)"; code=$?
  check "uninstalled, stock $THIS is back byte for byte (nothing of $OLDREL left either)" "[ $code = 0 ] && same_as_stock"
  rm -rf "$T/older"; git -C "$REPO" worktree prune
else
  echo "(an older release on an older Dispatcharr: skipped, set PREVIOUS_ARCHIVE to one)"
fi

echo "The recordings worker (dvr-worker.sh, against a pretend systemd):"
UNITS="$T/units"; FAKE="$T/fakebin"; mkdir -p "$UNITS" "$FAKE" "$T/dvr-state"
# The Celery service as debian_install.sh writes it
cat > "$UNITS/dispatcharr-celery.service" <<'UNIT'
[Unit]
Description=Celery Worker for Dispatcharr
After=network.target redis-server.service
Requires=dispatcharr.service

[Service]
User=dispatcharr
WorkingDirectory=/opt/dispatcharr
EnvironmentFile=/opt/dispatcharr/.env
Environment="CELERY_BROKER_URL=redis://localhost:6379/0"
ExecStart=/opt/dispatcharr/env/bin/celery -A dispatcharr worker -l info
Restart=always
KillMode=mixed
SyslogIdentifier=dispatcharr-celery
[Install]
WantedBy=multi-user.target
UNIT
printf '[Service]\nExecStart=/opt/dispatcharr/env/bin/celery -A dispatcharr beat -l info\n' > "$UNITS/dispatcharr-celerybeat.service"
cat > "$FAKE/systemctl" <<FAKE
#!/bin/bash
echo "\$*" >> "$T/systemctl.log"
case "\$1" in
  list-units) for f in "$UNITS"/dispatcharr*.service; do echo "\$(basename "\$f") loaded active running x"; done ;;
  show) echo "$UNITS/\${@: -1}" ;;
  is-active) [ -e "$T/dvr-dead" ] && exit 3 ;;
esac
exit 0
FAKE
chmod +x "$FAKE/systemctl"
dvr() { PATH="$FAKE:$PATH" SYSTEMD_DIR="$UNITS" bash "$REPO/fork/patcher/dvr-worker.sh" "$@" "$T/dvr-state" "$SLUG" 2>&1; }
out="$(dvr add)"
check "a worker of its own for recordings, copied from the install's Celery service" \
  "grep -q '^ExecStart=/opt/dispatcharr/env/bin/celery -A dispatcharr worker -l info -Q dvr -n dvr@%%h --pool=threads --concurrency=20$' '$UNITS/dispatcharr-celery-dvr.service' && grep -q '^User=dispatcharr' '$UNITS/dispatcharr-celery-dvr.service'"
check "...and the normal worker leaves recordings to it" \
  "grep -q 'DISPATCHARR_DVR_ON_DEFAULT_WORKER=false' '$UNITS/dispatcharr-celery.service.d/$SLUG-dvr.conf'"
check "...the stock service untouched" "grep -q 'worker -l info$' '$UNITS/dispatcharr-celery.service' && ! grep -q dvr '$UNITS/dispatcharr-celery.service'"
out="$(dvr add)"
check "added again (an update) it is the same one, not a copy of itself" \
  "[ \$(grep -c -- '-Q dvr' '$UNITS/dispatcharr-celery-dvr.service') = 1 ]"
out="$(dvr running)"
check "started after the restart, and the drop-in kept while it runs" \
  "echo \"\$out\" | grep -q 'is running' && grep -q 'restart dispatcharr-celery-dvr.service' '$T/systemctl.log' && [ -f '$UNITS/dispatcharr-celery.service.d/$SLUG-dvr.conf' ]"
touch "$T/dvr-dead"
out="$(dvr running)"
check "one that does not start takes its drop-in back: the normal worker runs recordings" \
  "echo \"\$out\" | grep -q 'did not start' && [ ! -e '$UNITS/dispatcharr-celery.service.d' ] && grep -q '^restart dispatcharr-celery.service' '$T/systemctl.log'"
rm -f "$T/dvr-dead"
out="$(dvr add)"
out="$(dvr remove)"
check "removed, nothing of it is left" \
  "[ ! -e '$UNITS/dispatcharr-celery-dvr.service' ] && [ ! -e '$UNITS/dispatcharr-celery.service.d' ] && [ ! -e '$T/dvr-state/dvr-worker' ]"

echo "The recordings worker on the Proxmox community script's install (a start script):"
CU="$T/cunits"; CFAKE="$T/cfake"; CST="$T/cdvr-state"; mkdir -p "$CU" "$CFAKE" "$CST" "$T/copt"
# As install/dispatcharr-install.sh writes them
printf '#!/usr/bin/env bash\ncd /opt/dispatcharr\nset -a\nsource .env\nset +a\nexec uv run celery -A dispatcharr worker -l info -c 4 -Q celery,recordings,dvr,default\n' > "$T/copt/start-celery.sh"
printf '#!/usr/bin/env bash\ncd /opt/dispatcharr\nexec uv run celery -A dispatcharr beat -l info\n' > "$T/copt/start-celerybeat.sh"
cp "$T/copt/start-celery.sh" "$T/start-celery.orig"
printf '[Unit]\nDescription=Dispatcharr Celery Worker\n\n[Service]\nType=simple\nWorkingDirectory=/opt/dispatcharr\nExecStart=%s\nRestart=on-failure\nUser=root\n' "$T/copt/start-celery.sh" > "$CU/dispatcharr-celery.service"
printf '[Service]\nExecStart=%s\n' "$T/copt/start-celerybeat.sh" > "$CU/dispatcharr-celerybeat.service"
cat > "$CFAKE/systemctl" <<FAKE
#!/bin/bash
case "\$1" in
  list-units) for f in "$CU"/dispatcharr*.service; do echo "\$(basename "\$f") loaded active running x"; done ;;
  show) echo "$CU/\${@: -1}" ;;
esac
exit 0
FAKE
chmod +x "$CFAKE/systemctl"
cdvr() { PATH="$CFAKE:$PATH" SYSTEMD_DIR="$CU" bash "$REPO/fork/patcher/dvr-worker.sh" "$@" "$CST" "$SLUG" 2>&1; }
out="$(cdvr add)"
check "its worker is found through the start script, and the recordings worker runs a copy on dvr" \
  "grep -q '^ExecStart=/bin/bash $CST/celery-dvr.sh$' '$CU/dispatcharr-celery-dvr.service' && grep -qx 'exec uv run celery -A dispatcharr worker -l info -Q dvr -n dvr@\$(hostname) --pool=threads --concurrency=20' '$CST/celery-dvr.sh' && grep -qx 'cd /opt/dispatcharr' '$CST/celery-dvr.sh'"
check "...the normal worker runs a copy without dvr, through a drop-in" \
  "grep -qx 'exec uv run celery -A dispatcharr worker -l info -c 4 -Q celery,recordings,default' '$CST/celery-main.sh' && grep -qx 'ExecStart=' '$CU/dispatcharr-celery.service.d/$SLUG-dvr.conf' && grep -qx 'ExecStart=/bin/bash $CST/celery-main.sh' '$CU/dispatcharr-celery.service.d/$SLUG-dvr.conf'"
check "...and the community script's own files are untouched" \
  "cmp -s '$T/copt/start-celery.sh' '$T/start-celery.orig' && grep -q '^ExecStart=$T/copt/start-celery.sh$' '$CU/dispatcharr-celery.service'"
out="$(cdvr remove)"
check "...removed, nothing of it is left and the worker runs its own script again" \
  "[ ! -e '$CU/dispatcharr-celery-dvr.service' ] && [ ! -e '$CU/dispatcharr-celery.service.d' ] && [ ! -e '$CST/celery-dvr.sh' ] && [ ! -e '$CST/celery-main.sh' ] && cmp -s '$T/copt/start-celery.sh' '$T/start-celery.orig'"

echo "The memory recorder (memory-recorder.sh, against a pretend systemd):"
MEM="$T/mem-state"; MEMFAKE="$T/memfake"; MEMAPP="$T/memapp"; mkdir -p "$MEM" "$MEMFAKE" "$MEMAPP/.venv/bin"
printf '#!/bin/bash\nexit 0\n' > "$MEMAPP/.venv/bin/python"; chmod +x "$MEMAPP/.venv/bin/python"
cat > "$MEMFAKE/systemctl" <<FAKE
#!/bin/bash
echo "\$*" >> "$T/memctl.log"
[ "\$1" = show ] && [ "\$3" = "-p" ] && [ "\$4" = User ] && echo "$(id -un)"
exit 0
FAKE
chmod +x "$MEMFAKE/systemctl"
mem() { PATH="$MEMFAKE:$PATH" SYSTEMD_DIR="$UNITS" MEMORY_HISTORY_DIR="$T/history" bash "$REPO/fork/patcher/memory-recorder.sh" "$@" 2>&1; }
out="$(mem add "$MEM" "$SLUG" "$MEMAPP")"
check "a service of its own, running the recorder from the install's Python as Dispatcharr's user" \
  "grep -q '^ExecStart=$MEMAPP/.venv/bin/python $MEMAPP/apps/proxy/live_proxy/memory_recorder.py --dir $T/history$' '$UNITS/$SLUG-memory.service' && grep -q '^User=$(id -un)$' '$UNITS/$SLUG-memory.service' && grep -q '^Restart=always$' '$UNITS/$SLUG-memory.service'"
check "...enabled for every boot and started" \
  "grep -q 'enable $SLUG-memory.service' '$T/memctl.log' && grep -q 'restart $SLUG-memory.service' '$T/memctl.log' && grep -q '^WantedBy=multi-user.target$' '$UNITS/$SLUG-memory.service'"
check "...its history folder there and the web app's to switch" "[ -d '$T/history' ] && [ \"\$(stat -c %U '$T/history')\" = '$(id -un)' ]"
echo '{"t": 1}' > "$T/history/2026-10-20.jsonl"
out="$(mem remove "$MEM" "$SLUG")"
check "removed, the service goes and the history stays" \
  "[ ! -e '$UNITS/$SLUG-memory.service' ] && grep -q 'disable --now $SLUG-memory.service' '$T/memctl.log' && [ -f '$T/history/2026-10-20.jsonl' ] && [ ! -e '$MEM/memory-recorder' ]"
out="$(mem remove "$MEM" "$SLUG")"
check "...and removing it twice is no error" "echo \"\$out\" | grep -qv 'rror'"

echo "The caption worker (captions.sh, against a pretend systemd, pip and card):"
CAP="$T/cap-state"; mkdir -p "$CAP/requests" "$T/models" "$T/capapp/apps/channels/captions"
touch "$T/capapp/apps/channels/captions/worker.py"
mkdir -p "$T/capvenv/bin"; printf '#!/bin/bash\nexit 0\n' > "$T/capvenv/bin/python"; chmod +x "$T/capvenv/bin/python"
printf '#!/bin/bash\nexit 0\n' > "$FAKE/uv"; printf '#!/bin/bash\nexit 0\n' > "$FAKE/curl"
printf '#!/bin/bash\nexit 1\n' > "$FAKE/nvidia-smi"; chmod +x "$FAKE/uv" "$FAKE/curl" "$FAKE/nvidia-smi"
cap() { PATH="$FAKE:$PATH" SYSTEMD_DIR="$UNITS" VENV="$T/capvenv" DISPATCHARR_MODELS_DIR="$T/models/m" \
        bash "$REPO/fork/patcher/captions.sh" "$1" "$CAP" "$SLUG" "$T/capapp" "${2:-}" 2>&1; }
cp "$REPO/fork/patcher/captions.sh" "$CAP/"
out="$(cap watch)"
check "a root watcher for the Subtitles tab's requests, and nothing installed yet" \
  "grep -q '^PathExists=$CAP/requests/captions$' '$UNITS/$SLUG-captions-request.path' && grep -q 'captions.sh request' '$UNITS/$SLUG-captions-request.service' && [ ! -e '$UNITS/$SLUG-captions.service' ]"
echo '{"action": "install"}' > "$CAP/requests/captions"
out="$(cap request)"
check "asked for, the worker is installed as the user Dispatcharr runs as, on 127.0.0.1" \
  "grep -q '^ExecStart=$T/capvenv/bin/python $T/capapp/apps/channels/captions/worker.py --host 127.0.0.1 --port 9725 --models $T/models/m/captions$' '$UNITS/$SLUG-captions.service' && [ ! -e '$CAP/requests/captions' ] && grep -q '\"state\": \"installed\"' '$CAP/captions-status.json'"
check "...without CUDA libraries when there is no NVIDIA card" "! grep -q 'NVIDIA card' '$CAP/captions-status.json' && grep -q '\"gpu\": false' '$CAP/captions-status.json'"
echo '{"action": "remove"}' > "$CAP/requests/captions"
out="$(cap request)"
check "asked to remove, the worker, its environment and its models go; the watcher stays" \
  "[ ! -e '$UNITS/$SLUG-captions.service' ] && [ ! -e '$T/capvenv' ] && [ ! -e '$T/models/m/captions' ] && [ -e '$UNITS/$SLUG-captions-request.path' ] && grep -q removed '$CAP/captions-status.json'"
out="$(cap remove all)"
check "uninstalling Dispatch More takes the watcher too" \
  "[ ! -e '$UNITS/$SLUG-captions-request.path' ] && [ ! -e '$UNITS/$SLUG-captions-request.service' ] && [ ! -e '$CAP/captions-status.json' ]"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
