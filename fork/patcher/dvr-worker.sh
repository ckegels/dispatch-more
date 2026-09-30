#!/bin/bash
# A Celery worker of its own for recordings, on a Linux/LXC (systemd) install only.
#
#   bash dvr-worker.sh add    STATE SLUG   # install.sh, after the files are in place
#   bash dvr-worker.sh remove STATE SLUG   # uninstall.sh, and install.sh when it is switched off
#
# Stock routes recordings to a `dvr` queue. The Docker image serves it with a worker of its
# own (-Q dvr, a thread pool of 20: a recording mostly waits while ffmpeg works). The Debian
# install (debian_install.sh) starts one worker without -Q; since v236 that worker listens to
# `dvr` too, so recordings run, but each one holds one of its slots -- one child process per
# CPU core -- for the whole programme. This adds what Docker has: `dispatcharr-celery-dvr`, a
# copy of the install's own Celery service started with -Q dvr on threads, and a drop-in that
# has the normal worker leave `dvr` to it, so the two do not compete for a recording.
# Never on Docker (install.sh calls this for the systemd layout only). SYSTEMD_DIR exists for
# the tests; on a server it is /etc/systemd/system.
set -euo pipefail

ACTION="${1:?add or remove}"
STATE="${2:?state directory}"
SLUG="${3:-dispatch-more}"
UNITS="${SYSTEMD_DIR:-/etc/systemd/system}"
DVR="dispatcharr-celery-dvr"
MARK="$STATE/dvr-worker"

remove() {
  [ -f "$MARK" ] || [ -f "$UNITS/$DVR.service" ] || return 0
  systemctl disable --now "$DVR.service" >/dev/null 2>&1 || true
  rm -f "$UNITS/$DVR.service"
  if [ -f "$MARK" ]; then
    CELERY="$(cat "$MARK")"
    rm -f "$UNITS/$CELERY.d/$SLUG-dvr.conf"
    rmdir "$UNITS/$CELERY.d" 2>/dev/null || true
    rm -f "$MARK"
  fi
  systemctl daemon-reload || true
  echo "Removed the recordings worker ($DVR); the Celery worker runs recordings again."
}

add() {
  # The install's own Celery worker: its ExecStart runs `celery ... worker` and is neither
  # beat nor a worker that was already given the dvr queue
  CELERY="" SOURCE=""
  for unit in $(systemctl list-units --type=service --all --no-legend 'dispatcharr*' | awk '{print $1}'); do
    [ "$unit" = "$DVR.service" ] && continue
    file="$(systemctl show -p FragmentPath --value "$unit" 2>/dev/null || true)"
    [ -n "$file" ] && [ -f "$file" ] || continue
    if grep -qE '^ExecStart=.*celery.* worker' "$file" && ! grep -qE -- '-Q +dvr|-Q +[a-z,]*dvr| beat' "$file"; then
      CELERY="$unit" SOURCE="$file"
      break
    fi
  done
  if [ -z "$CELERY" ]; then
    echo "Note: no Celery worker service found to copy; recordings stay on the one worker."
    return 0
  fi
  sed -e "s|^Description=.*|Description=Dispatcharr recordings: the Celery dvr queue on threads (added by $SLUG)|" \
      -e "s|^SyslogIdentifier=.*|SyslogIdentifier=$DVR|" \
      -e "/^ExecStart=.*celery/ s|\$| -Q dvr -n dvr@%%h --pool=threads --concurrency=20|" \
      -e "s|^Restart=.*|Restart=always|" \
      "$SOURCE" > "$UNITS/$DVR.service"
  grep -q '^Restart=' "$UNITS/$DVR.service" || sed -i 's|^\[Service\]|[Service]\nRestart=always|' "$UNITS/$DVR.service"
  mkdir -p "$UNITS/$CELERY.d"
  printf '# Added by %s: recordings are run by %s\n[Service]\nEnvironment="DISPATCHARR_DVR_ON_DEFAULT_WORKER=false"\n' \
    "$SLUG" "$DVR" > "$UNITS/$CELERY.d/$SLUG-dvr.conf"
  echo "$CELERY" > "$MARK"
  systemctl daemon-reload
  systemctl enable "$DVR.service" >/dev/null 2>&1 || true
  echo "Added a worker of its own for recordings ($DVR, up to 20 at once)."
}

case "$ACTION" in
  add) add ;;
  remove) remove ;;
  *) echo "Unknown action: $ACTION"; exit 2 ;;
esac
