#!/bin/bash
# The whole way a user goes, on a real machine: a clean Debian 12 container with systemd as its
# first process, Dispatcharr installed by its own installer, Dispatch More on and off. Slow (it
# installs Dispatcharr, PyTorch included, twice) and needs Docker; test-patcher.sh is the quick
# check of the same files, this is the one that runs the services.
#
#   fork/patcher/test-real.sh <older release .tar.gz> <this release .tar.gz>
#
#   1. The one command on a machine without Dispatcharr: the older release's Dispatcharr is
#      installed first (debian_install.sh, pinned), then the older release.
#   2. Dispatcharr updated to this release's version with its own installer (git reset --hard,
#      pull, migrate, frontend build): the older release's added files are left behind.
#   3. The one command again: this release, over what the update left.
#   4. The page's Uninstall (an admin's POST, carried out by the watcher): stock, the services up.
#   5. The one command once more, then the one command's --uninstall: stock again.
#
# UPGRADE=1: steps 2 and 3 are the one command's --upgrade instead (Dispatch More off,
# Dispatcharr updated to this release's version by its own installer, Dispatch More on).
#
# Stock is checked with git in /opt/dispatcharr: nothing but what Dispatcharr's own installer
# writes there (.env, uwsgi-debian.ini, package-lock.json) may differ. KEEP=1 leaves the
# container running afterwards.
set -uo pipefail

OLD="$(realpath "${1:?usage: test-real.sh <older release .tar.gz> <this release .tar.gz>}")"
NEW="$(realpath "${2:?usage: test-real.sh <older release .tar.gz> <this release .tar.gz>}")"
REPO="$(git rev-parse --show-toplevel)"
NAME="dm-real-$$"
IMAGE="dispatch-more-test-systemd:12"
LOG="${LOG:-$(mktemp -t dm-real-XXXX.log)}"
PASS=0 FAIL=0

ok()  { echo "  ok    $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL  $1"; FAIL=$((FAIL + 1)); }
check() { if eval "$2" >>"$LOG" 2>&1; then ok "$1"; else bad "$1"; fi; }
inside() { docker exec "$NAME" bash -c "$1"; }
version_of() { tar -xzOf "$1" dispatch-more/manifest.json | python3 -c "import json,sys;m=json.load(sys.stdin);print(m[sys.argv[1]])" "$2"; }
OLD_V="$(version_of "$OLD" for_dispatcharr)" OLD_R="$(version_of "$OLD" release)"
NEW_V="$(version_of "$NEW" for_dispatcharr)" NEW_R="$(version_of "$NEW" release)"
echo "Log: $LOG"
echo "$OLD_R (Dispatcharr $OLD_V) -> Dispatcharr $NEW_V -> $NEW_R"

cleanup() { [ -n "${KEEP:-}" ] || docker rm -f "$NAME" >/dev/null 2>&1; }
trap cleanup EXIT

# A Debian 12 with systemd, made once
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "Making $IMAGE..."
  docker run --name "$NAME-base" debian:12 bash -c \
    "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq systemd systemd-sysv curl ca-certificates sudo git python3 >/dev/null" >>"$LOG" 2>&1 \
    && docker commit "$NAME-base" "$IMAGE" >/dev/null; docker rm -f "$NAME-base" >/dev/null 2>&1
fi
# A private cgroup namespace and no /.dockerenv: what an LXC looks like from inside, so the
# installers take it for the Linux machine it stands in for and not for a Docker image
docker run -d --name "$NAME" --privileged --cgroupns=private \
  --tmpfs /run --tmpfs /run/lock "$IMAGE" /sbin/init >/dev/null
docker exec "$NAME" rm -f /.dockerenv
for _ in $(seq 1 30); do inside "systemctl is-system-running 2>/dev/null | grep -qE 'running|degraded'" && break; sleep 1; done
inside "[ ! -f /.dockerenv ] && ! grep -qs docker /proc/1/cgroup" || { echo "The container still looks like Docker from inside"; exit 1; }
docker cp "$OLD" "$NAME:/root/old.tar.gz"; docker cp "$NEW" "$NAME:/root/new.tar.gz"
docker cp "$REPO/fork/patcher/quick-install.sh" "$NAME:/root/quick-install.sh"
docker cp "$REPO/fork/scripts/dispatcharr-shell.sh" "$NAME:/root/dispatcharr-shell.sh"

up() {  # every Dispatcharr service active (the recordings worker's is checked on its own)
  inside 'for _ in $(seq 1 60); do bad=0; for s in dispatcharr dispatcharr-celery dispatcharr-celerybeat dispatcharr-daphne; do systemctl is-active -q $s || bad=1; done; [ $bad = 0 ] && break; sleep 2; done; [ $bad = 0 ]'
}
answers() {  # the page answers (nginx -> uWSGI) as Dispatcharr $1 with release $2 ('' = stock)
  local out
  out="$(inside "for _ in \$(seq 1 150); do curl -fsS localhost:9191/api/core/version/ 2>/dev/null | grep -q '\"version\": *\"$1\"' && break; sleep 2; done; curl -fsS localhost:9191/api/core/version/")"
  echo "$out" >>"$LOG"
  echo "$out" | grep -q "\"version\": *\"$1\"" || return 1
  if [ -n "$2" ]; then echo "$out" | grep -q "\"build\": *\"Dispatch More $2\""; else ! echo "$out" | grep -q "Dispatch More"; fi
}
stock_files() {  # git sees nothing but Dispatcharr's own installer's files (git failing is no answer)
  local out
  out="$(inside "git -c safe.directory=/opt/dispatcharr -C /opt/dispatcharr status --porcelain --untracked-files=all")" || return 2
  echo "$out" | grep -vE ' (\.env|uwsgi-debian\.ini|frontend/package-lock\.json)$' >>"$LOG"
  ! echo "$out" | grep -vE ' (\.env|uwsgi-debian\.ini|frontend/package-lock\.json)$' | grep -q .
}
left_behind() {  # the update left untracked files of the older release (and only that is "not stock")
  inside "git -c safe.directory=/opt/dispatcharr -C /opt/dispatcharr status --porcelain --untracked-files=all" | grep -q '^?? .fork-install.json'
}
fork_units_gone() {
  inside "! ls /etc/systemd/system/ | grep -q 'dispatch-more' && ! systemctl list-units --all --no-legend 'dispatch-more*' | grep -q ."
}
no_tracebacks_since() {  # nothing Dispatcharr's services logged since $1 is a traceback
  ! inside "journalctl --since '$1' -u 'dispatcharr*' --no-pager" | grep -q "Traceback (most recent call last)"
}
admin_token() {
  inside "bash /root/dispatcharr-shell.sh shell -c \"from apps.accounts.models import User; u=User.objects.filter(username='claude-test').first() or User.objects.create_superuser('claude-test', 'x@example.invalid', 'claude-test-pass', user_level=10); print('made')\"" >>"$LOG" 2>&1
  inside "curl -fsS -X POST -H 'Content-Type: application/json' -d '{\"username\":\"claude-test\",\"password\":\"claude-test-pass\"}' localhost:9191/api/accounts/token/" | python3 -c "import json,sys;print(json.load(sys.stdin)['access'])"
}
get() { inside "curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer $1' localhost:9191$2"; }

echo "1. The one command on a machine without Dispatcharr ($OLD_R, Dispatcharr $OLD_V):"
inside "DISPATCH_MORE_ARCHIVE=/root/old.tar.gz DISPATCH_MORE_DISPATCHARR_ANSWER='I understand' DISPATCH_MORE_WAIT=0 bash /root/quick-install.sh" >>"$LOG" 2>&1
check "Dispatcharr $OLD_V and $OLD_R are on, every service up" "up && answers $OLD_V $OLD_R"

if [ -n "${UPGRADE:-}" ]; then
echo "2-3. The one command's --upgrade: Dispatcharr $OLD_V -> $NEW_V and $NEW_R, in one go:"
SINCE="$(inside 'date "+%Y-%m-%d %H:%M:%S"')"
inside "DISPATCH_MORE_ARCHIVE=/root/new.tar.gz DISPATCH_MORE_DISPATCHARR_ANSWER='I understand' DISPATCH_MORE_WAIT=0 bash /root/quick-install.sh --upgrade" >>"$LOG" 2>&1
check "$NEW_R is on Dispatcharr $NEW_V, every service up" "up && answers $NEW_V $NEW_R"
check "...Dispatcharr's own installer put exactly $NEW_V there" "inside 'git -c safe.directory=/opt/dispatcharr -C /opt/dispatcharr describe --tags' | grep -qx v$NEW_V"
check "...it took $OLD_R off first, on the Dispatcharr it was made for" "grep -q '$OLD_R uninstalled: stock Dispatcharr $OLD_V is back' '$LOG'"
else
echo "2. Dispatcharr updated to $NEW_V by its own installer:"
inside "curl -fsSL https://raw.githubusercontent.com/Dispatcharr/Dispatcharr/v$NEW_V/debian_install.sh -o /root/debian_install.sh && sed -i 's|^\( *\)DISPATCH_BRANCH=\"main\"|\1DISPATCH_BRANCH=\"v$NEW_V\"|' /root/debian_install.sh && printf 'I understand\n' | bash /root/debian_install.sh" >>"$LOG" 2>&1
# The installer ends with "systemctl enable --now", which starts what is stopped and leaves what
# runs alone: the old version's processes keep answering until something restarts them (the
# first clean run waited five minutes on the old code). A user's reboot or restart does that.
inside "systemctl restart dispatcharr dispatcharr-celery dispatcharr-celerybeat dispatcharr-daphne" >>"$LOG" 2>&1
check "Dispatcharr $NEW_V runs, stock (the update put its files back)" "up && answers $NEW_V ''"
check "...with $OLD_R's added files and record left behind, as the update leaves them" "left_behind"

echo "3. The one command again ($NEW_R):"
SINCE="$(inside 'date "+%Y-%m-%d %H:%M:%S"')"
inside "DISPATCH_MORE_ARCHIVE=/root/new.tar.gz bash /root/quick-install.sh" >>"$LOG" 2>&1
check "$NEW_R is on, every service up" "up && answers $NEW_V $NEW_R"
check "...it said it took off only what was still $OLD_R's" "grep -q 'updated from $OLD_V to $NEW_V' '$LOG'"
fi
TOKEN="$(admin_token)"
check "...an admin can log in (for the checks below)" "[ -n '$TOKEN' ]"
for path in /api/core/modified-build/ /api/core/capabilities/ /api/core/app-sync/ /api/core/arrtv/ \
            /api/channels/stream-check/ /proxy/diagnostics/ /proxy/diagnostics/memory/ \
            /api/channels/channels/ /api/epg/grid/ /api/m3u/accounts/; do
  check "...$path answers 200" "[ \"\$(get '$TOKEN' $path)\" = 200 ]"
done
sleep 20
check "...no traceback in any Dispatcharr service's log since installing" "no_tracebacks_since '$SINCE'"

echo "4. The page's Uninstall:"
inside "curl -fsS -X POST -H 'Authorization: Bearer $TOKEN' localhost:9191/api/core/modified-build/uninstall/" >>"$LOG" 2>&1
inside 'for _ in $(seq 1 90); do [ -f /opt/dispatcharr/.fork-install.json ] || break; sleep 2; done' >>"$LOG" 2>&1
check "stock Dispatcharr $NEW_V is back, every service up" "up && answers $NEW_V ''"
check "...git sees only Dispatcharr's own installer's files" "stock_files"
check "...no unit of Dispatch More is left" "fork_units_gone"
check "...the build's schedules are out of Celery beat" "inside \"bash /root/dispatcharr-shell.sh shell -c \\\"from django_celery_beat.models import PeriodicTask as P; print('LEFT=%d' % P.objects.filter(name__in=['stream-check-tick','epg-grab-tick','show-groups-tick','show-groups-look-up']).count())\\\"\" | grep -qx 'LEFT=0'"

echo "5. The one command, then its --uninstall:"
inside "DISPATCH_MORE_ARCHIVE=/root/new.tar.gz bash /root/quick-install.sh" >>"$LOG" 2>&1
check "$NEW_R is on again" "up && answers $NEW_V $NEW_R"
inside "DISPATCH_MORE_ARCHIVE=/root/new.tar.gz bash /root/quick-install.sh --uninstall" >>"$LOG" 2>&1
check "uninstalled: stock $NEW_V, every service up" "up && answers $NEW_V ''"
check "...git sees only Dispatcharr's own installer's files" "stock_files"
check "...no unit of Dispatch More is left" "fork_units_gone"

echo
echo "$PASS passed, $FAIL failed (log: $LOG)"
[ "$FAIL" = 0 ]
