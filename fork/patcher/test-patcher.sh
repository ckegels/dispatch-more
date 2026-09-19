#!/bin/bash
# Tests the patcher on real stock copies of Dispatcharr: install, install again, refusing the
# wrong version and changed files, upgrading, uninstalling back to byte-for-byte stock, and
# the Docker start script. Needs a built release:
#
#   fork/patcher/test-patcher.sh fork/patcher/out/dispatch-more-v99-dispatcharr-0.31.0.tar.gz
set -uo pipefail

ARCHIVE="$(realpath "${1:?usage: test-patcher.sh <release .tar.gz>}")"
BASE="${BASE:-bcbb68c4}"
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

echo "No built frontend before:"
fresh; rm -rf "$STATE" "$T/app/frontend/dist"
bash "$T/pkg/install.sh" --app "$T/app" --no-systemd >/dev/null 2>&1
bash "$STATE/uninstall.sh" --app "$T/app" --no-systemd >/dev/null 2>&1
check "uninstalling takes the release's frontend away again" "[ ! -e '$T/app/frontend/dist' ]"

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
out="$(start)"
check "an image of another Dispatcharr starts as stock, and says why" "echo \"\$out\" | grep -q 'not applied' && echo \"\$out\" | tail -1 | grep -q started"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
