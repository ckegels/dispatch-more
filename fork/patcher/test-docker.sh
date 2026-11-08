#!/bin/bash
# The Docker way, with Dispatcharr's official images: the older release on its Dispatcharr's
# image, the image updated to this release's Dispatcharr (the entrypoint must start it stock and
# say why), this release put on with the one command and kept across a recreate, then the one
# command's --uninstall: /app as the image has it, static/ included, and stock after a recreate.
#
#   fork/patcher/test-docker.sh <older release .tar.gz> <this release .tar.gz>
#
# Step 5 has a newer image find its release by itself: the start script of this release (on
# the older one's /data) fetches the release made for the newer image from GitHub -- the real
# one, so it needs one published for that Dispatcharr version.
#
# Needs Docker and pulls ghcr.io/dispatcharr/dispatcharr:<version> for both. About 2 min once pulled. KEEP=1
# leaves the container and its volume. test-real.sh is the same for a Linux machine.
set -uo pipefail
OLD="$(realpath "${1:?usage: test-docker.sh <older release .tar.gz> <this release .tar.gz>}")"
NEW="$(realpath "${2:?usage: test-docker.sh <older release .tar.gz> <this release .tar.gz>}")"
R="$(git rev-parse --show-toplevel)"
S="$(mktemp -d -t dm-docker-XXXX)"
version_of() { tar -xzOf "$1" dispatch-more/manifest.json | python3 -c "import json,sys;m=json.load(sys.stdin);print(m[sys.argv[1]])" "$2"; }
OLD_V="$(version_of "$OLD" for_dispatcharr)" OLD_R="$(version_of "$OLD" release)"
NEW_V="$(version_of "$NEW" for_dispatcharr)" NEW_R="$(version_of "$NEW" release)"
OLD_IMAGE="ghcr.io/dispatcharr/dispatcharr:$OLD_V" NEW_IMAGE="ghcr.io/dispatcharr/dispatcharr:$NEW_V"
echo "$OLD_R (Dispatcharr $OLD_V) -> image $NEW_V -> $NEW_R"
N="dm-docker-$$"; VOL="dm-docker-$$-data"; LOG="${LOG:-$(mktemp -t dm-docker-XXXX.log)}"; : > "$LOG"
PASS=0 FAIL=0
ok()  { echo "  ok    $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL  $1"; FAIL=$((FAIL + 1)); }
check() { if eval "$2" >>"$LOG" 2>&1; then ok "$1"; else bad "$1"; fi; }
inside() { docker exec "$N" bash -c "$1"; }
start() {  # start <image> [entrypoint]: a new container on the same /data
  docker rm -f $N >/dev/null 2>&1
  if [ -n "${2:-}" ]; then
    docker run -d --name $N -v $VOL:/data --entrypoint /bin/bash "$1" /data/dispatch-more/docker-entrypoint.sh >/dev/null
  else
    docker run -d --name $N -v $VOL:/data "$1" >/dev/null
  fi
}
answers() {  # answers <version> <release or ''>
  local out=""
  for _ in $(seq 1 150); do
    out="$(inside 'curl -fsS localhost:9191/api/core/version/' 2>/dev/null)" && echo "$out" | grep -q "\"version\": *\"$1\"" && break
    sleep 2
  done
  echo "answers: $out" >>"$LOG"
  echo "$out" | grep -q "\"version\": *\"$1\"" || return 1
  if [ -n "$2" ]; then echo "$out" | grep -q "\"build\": *\"Dispatch More $2\""; else ! echo "$out" | grep -q "Dispatch More"; fi
}
logs_say() { docker logs $N >$S/docker-logs.txt 2>&1; grep -q "$1" $S/docker-logs.txt; }  # no pipe: grep -q would SIGPIPE docker logs under pipefail
static_is_stock() {  # every file of the image's frontend is in static/ as the image has it
  hashes | grep '  ./static/' | LC_ALL=C sort -k2 > $S/have.txt
  grep '  ./frontend/dist/' $S/docker-image.txt | sed 's#  ./frontend/dist/#  ./static/#' | LC_ALL=C sort -k2 > $S/want.txt
  ! LC_ALL=C join -1 2 -2 2 -a 1 -e MISSING -o 0,1.1,2.1 $S/want.txt $S/have.txt | awk '$2!=$3' | grep . 
}
hashes() { inside 'cd /app && find . -type f ! -path "*/__pycache__/*" ! -name "*.pyc" -print0 | sort -z | xargs -0 sha256sum'; }
one_command() {  # one_command <archive> [--uninstall]
  docker cp "$1" $N:/tmp/release.tar.gz; docker cp "$R/fork/patcher/quick-install.sh" $N:/tmp/quick-install.sh
  inside "DISPATCH_MORE_ARCHIVE=/tmp/release.tar.gz bash /tmp/quick-install.sh ${2:-}" >>"$LOG" 2>&1
}
trap '[ -n "${KEEP:-}" ] || { docker rm -f $N >/dev/null 2>&1; docker volume rm $VOL >/dev/null 2>&1; rm -rf "$S"; }' EXIT

docker pull -q "$OLD_IMAGE" >/dev/null; docker pull -q "$NEW_IMAGE" >/dev/null

echo "1. Dispatcharr $OLD_V from its image, $OLD_R with the one command, restart:"
start $OLD_IMAGE
check "stock $OLD_V answers" "answers $OLD_V ''"
one_command "$OLD"; docker restart $N >/dev/null
check "$OLD_R answers after the restart" "answers $OLD_V $OLD_R"
start $OLD_IMAGE entrypoint
check "...and again in a new container with the entrypoint" "answers $OLD_V $OLD_R"

echo "2. The image updated to $NEW_V, the entrypoint still set ($OLD_R in /data):"
start $NEW_IMAGE entrypoint
check "stock $NEW_V answers" "answers $NEW_V ''"
check "...the log says the release was not applied" "logs_say 'Dispatch More: not applied'"
docker run --rm --entrypoint bash $NEW_IMAGE -c 'cd /app && find . -type f ! -path "*/__pycache__/*" ! -name "*.pyc" -print0 | sort -z | xargs -0 sha256sum' > $S/docker-image.txt
hashes > $S/docker-stock-run.txt
check ".../app is the image's own, file for file (static/ apart: collectstatic writes it)" "diff <(grep -v '  ./static/' $S/docker-image.txt) <(grep -v '  ./static/' $S/docker-stock-run.txt)"

echo "3. $NEW_R with the one command, restart:"
one_command "$NEW"
# A new image's /app is stock: the older release's record in /data must not have it "taken off"
# (that would put $OLD_V's originals over $NEW_V's files)
check "...over the image's own files, nothing of $OLD_R taken off" "grep -q 'Dispatch More $NEW_R installed over Dispatcharr $NEW_V' $LOG && ! grep -q 'taking it off first' $LOG"
docker restart $N >/dev/null
check "$NEW_R answers after the restart" "answers $NEW_V $NEW_R"
start $NEW_IMAGE entrypoint
check "...and in a new container with the entrypoint" "answers $NEW_V $NEW_R"
check "...no traceback in the container's log" "! logs_say 'Traceback (most recent call last)'"

echo "4. The one command's --uninstall, restart, recreate:"
one_command "$NEW" --uninstall
docker restart $N >/dev/null
check "stock $NEW_V answers after the restart" "answers $NEW_V ''"
hashes > $S/docker-after.txt
check ".../app is the image's own, file for file (static/ apart)" "diff <(grep -v '  ./static/' $S/docker-image.txt) <(grep -v '  ./static/' $S/docker-after.txt)"
check "...static/ serves the image's frontend, none of the release's" "static_is_stock && ! grep -q 'ArrTvSettings' $S/have.txt"
start $NEW_IMAGE entrypoint
check "...and stays stock in a new container with the entrypoint" "answers $NEW_V ''"
check "...the log says uninstalled" "logs_say 'Dispatch More: uninstalled'"

echo "5. A newer image finds its release by itself (from GitHub):"
docker rm -f $N >/dev/null 2>&1; docker volume rm $VOL >/dev/null 2>&1
start $OLD_IMAGE
check "stock $OLD_V answers" "answers $OLD_V ''"
one_command "$OLD"
# This release's start script and one command on the older one's /data: what a release for the
# older Dispatcharr that has them leaves there
tar -xzOf "$NEW" dispatch-more/docker-entrypoint.sh | docker exec -i $N bash -c 'cat > /data/dispatch-more/docker-entrypoint.sh'
tar -xzOf "$NEW" dispatch-more/quick-install.sh | docker exec -i $N bash -c 'cat > /data/dispatch-more/quick-install.sh'
start $NEW_IMAGE entrypoint
PUBLISHED="$(curl -fsSL https://api.github.com/repos/ckegels/dispatch-more/releases?per_page=30 | python3 -c "
import json, sys
for r in json.load(sys.stdin):
    if any(a['name'].endswith('-dispatcharr-$NEW_V.tar.gz') for a in r.get('assets', [])):
        print(r['tag_name']); break")"
check "the release published for $NEW_V (${PUBLISHED:-none}) answers, with nothing run by hand" "[ -n '$PUBLISHED' ] && answers $NEW_V '$PUBLISHED'"
check "...the log says it looked for it" "logs_say 'looking for the one made for it'"

echo
echo "$PASS passed, $FAIL failed (log: $LOG)"
[ "$FAIL" = 0 ]
