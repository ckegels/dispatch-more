#!/bin/bash
# Take Dispatch More off and put stock Dispatcharr back: every file it changed is restored,
# every file it added removed, and the stock frontend returned. Settings only this build uses
# stay in the database, where stock Dispatcharr ignores them.
#
#   sudo bash /var/lib/dispatch-more/uninstall.sh
#
# Also what the page's Uninstall button runs, through the watcher install.sh set up.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SLUG="$(sed -n 's/.*"slug": "\([^"]*\)".*/\1/p' "$HERE/manifest.json" 2>/dev/null | head -1)"
SLUG="${SLUG:-dispatch-more}"
APP="" LAYOUT="" RESTART=1 SYSTEMD=1
while [ $# -gt 0 ]; do
  case "$1" in
    --app) APP="$2"; shift 2 ;;
    --docker) LAYOUT=docker; shift ;;
    --no-restart) RESTART=0; shift ;;
    --no-systemd) SYSTEMD=0; shift ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done
if [ -z "$LAYOUT" ]; then
  if [ -f /.dockerenv ] || grep -qs docker /proc/1/cgroup; then LAYOUT=docker; else LAYOUT=systemd; fi
fi
[ -n "$APP" ] || { if [ "$LAYOUT" = docker ]; then APP=/app; else APP=/opt/dispatcharr; fi; }
STATE="${STATE:-$HERE}"

PY=""
for candidate in "$APP/.venv/bin/python" "$APP/env/bin/python" "$(command -v python3 || true)"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then PY="$candidate"; break; fi
done
# Celery beat keeps the build's schedule in the database, where putting the files back would
# leave it: stock would be sent a task it does not have every five minutes. The page's button
# takes it out already; by hand it is done here, with the services' own environment (the
# database password lives in the systemd unit on a Debian install).
if [ "$LAYOUT" = systemd ] && [ "$SYSTEMD" = 1 ]; then
  (
    cd "$APP"
    set -a; [ -f .env ] && . ./.env; set +a
    UNIT="$(systemctl list-units --type=service --all --no-legend 'dispatcharr*' | awk '{print $1}' | head -1)"
    if [ -n "$UNIT" ]; then
      while IFS= read -r line; do [ -n "$line" ] && export "$line"; done < <(systemctl show "$UNIT" -p Environment --value | tr ' ' '\n')
    fi
    "$PY" manage.py shell -c "from django_celery_beat.models import PeriodicTask; PeriodicTask.objects.filter(name__in=['stream-check-tick']).delete()" >/dev/null 2>&1
  ) || echo "Note: could not take the build's schedule out of Celery beat; stock may log 'unregistered task stream_check_tick'."
fi

"$PY" "$STATE/patch.py" uninstall --app "$APP" --state "$STATE"

if [ "$LAYOUT" = systemd ] && [ "$SYSTEMD" = 1 ]; then
  # Reading the journal was only for Diagnostics -> Logs: taken away again if it was given
  if [ -f "$STATE/journal-group-added" ]; then
    gpasswd -d "$(cat "$STATE/journal-group-added")" systemd-journal >/dev/null 2>&1 || true
    rm -f "$STATE/journal-group-added"
  fi
  systemctl disable "$SLUG-uninstall.path" --no-block >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/$SLUG-uninstall.path" "/etc/systemd/system/$SLUG-uninstall.service"
  systemctl daemon-reload || true
  if [ "$RESTART" = 1 ]; then
    SERVICES="$(systemctl list-units --type=service --all --no-legend 'dispatcharr*' | awk '{print $1}' || true)"
    [ -n "$SERVICES" ] && systemctl restart $SERVICES && echo "Restarted: $SERVICES"
  fi
fi
