#!/bin/bash
# Install Dispatch More over an existing Dispatcharr: on Linux or an LXC (systemd), or inside
# Docker (see docker-entrypoint.sh, which runs this at every start of the container).
#
#   sudo bash install.sh                     # finds Dispatcharr in /opt/dispatcharr
#   sudo bash install.sh --app /srv/dispatcharr
#
# Not official Dispatcharr. To go back to stock: Settings -> System -> Modified build, or
# sudo bash /var/lib/dispatch-more/uninstall.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SLUG="$(sed -n 's/.*"slug": "\([^"]*\)".*/\1/p' "$HERE/manifest.json" | head -1)"
SLUG="${SLUG:-dispatch-more}"
APP="" LAYOUT="" RESTART=1 FORCE="" SYSTEMD=1
while [ $# -gt 0 ]; do
  case "$1" in
    --app) APP="$2"; shift 2 ;;
    --docker) LAYOUT=docker; shift ;;
    --no-restart) RESTART=0; shift ;;
    --no-systemd) SYSTEMD=0; shift ;;   # neither the uninstall watcher nor a restart
    --force) FORCE=--force; shift ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done
if [ -z "$LAYOUT" ]; then
  if [ -f /.dockerenv ] || grep -qs docker /proc/1/cgroup; then LAYOUT=docker; else LAYOUT=systemd; fi
fi
if [ -z "$APP" ]; then
  if [ "$LAYOUT" = docker ]; then APP=/app; else APP=/opt/dispatcharr; fi
fi
if [ -z "${STATE:-}" ]; then
  if [ "$LAYOUT" = docker ]; then STATE="/data/$SLUG"; else STATE="/var/lib/$SLUG"; fi
fi
[ -f "$APP/version.py" ] || { echo "No Dispatcharr at $APP (use --app to say where it is)."; exit 1; }
if [ "$(id -u)" != 0 ] && [ -z "${ALLOW_NOT_ROOT:-}" ]; then
  echo "Run as root: the files belong to the installation, and its services are restarted."; exit 1
fi

# Dispatcharr's own Python, which is sure to be there
PY=""
for candidate in "$APP/.venv/bin/python" "$APP/env/bin/python" "$(command -v python3 || true)"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then PY="$candidate"; break; fi
done
[ -n "$PY" ] || { echo "No Python found to install with."; exit 1; }

# Nothing new is needed, but Stream Check judges streams with ffprobe and ffmpeg
for tool in ffmpeg ffprobe; do
  command -v "$tool" >/dev/null || echo "Note: $tool is not installed; Stream Check will judge streams less well without it."
done

set +e
"$PY" "$HERE/patch.py" install --app "$APP" --state "$STATE" --package "$HERE" --layout "$LAYOUT" $FORCE
CODE=$?
set -e
[ "$CODE" = 10 ] && exit 0
[ "$CODE" = 0 ] || exit "$CODE"

if [ "$LAYOUT" = systemd ] && [ "$SYSTEMD" = 1 ]; then
  # The page's Uninstall button leaves a request; this root-owned watcher carries it out
  cat > "/etc/systemd/system/$SLUG-uninstall.path" <<UNIT
[Unit]
Description=Uninstall $SLUG when the Dispatcharr page asks for it

[Path]
PathExists=$STATE/requests/uninstall

[Install]
WantedBy=multi-user.target
UNIT
  cat > "/etc/systemd/system/$SLUG-uninstall.service" <<UNIT
[Unit]
Description=Uninstall $SLUG and put stock Dispatcharr back

[Service]
Type=oneshot
ExecStart=/bin/bash $STATE/uninstall.sh --app $APP
UNIT
  systemctl daemon-reload
  systemctl enable --now "$SLUG-uninstall.path" >/dev/null 2>&1 || echo "Note: the uninstall watcher could not be started; the page's button will not work."

  # Diagnostics -> Logs reads the systemd journal; the user Dispatcharr runs as needs to be in
  # the systemd-journal group for that (root needs nothing). Noted, so uninstalling undoes it.
  RUNS_AS="$(systemctl show dispatcharr -p User --value 2>/dev/null || true)"
  if [ -n "$RUNS_AS" ] && [ "$RUNS_AS" != root ] && getent group systemd-journal >/dev/null \
     && ! id -nG "$RUNS_AS" 2>/dev/null | tr ' ' '\n' | grep -qx systemd-journal; then
    usermod -aG systemd-journal "$RUNS_AS" && echo "$RUNS_AS" > "$STATE/journal-group-added" \
      && echo "Let $RUNS_AS read the system journal, for Diagnostics -> Logs."
  fi

  if [ "$RESTART" = 1 ]; then
    SERVICES="$(systemctl list-units --type=service --all --no-legend 'dispatcharr*' | awk '{print $1}' | grep -v "^$SLUG" || true)"
    if [ -n "$SERVICES" ]; then
      systemctl stop $SERVICES
      # Nothing streams while stopped: connection slots left by channels that could not clean
      # up are cleared (Redis keeps them across restarts)
      if command -v redis-cli >/dev/null; then
        for pattern in "profile_connections:*" "channel_stream:*" "stream_profile:*" \
                       "profile_credential_release:*" "server_group_connections:*"; do
          redis-cli --scan --pattern "$pattern" | xargs -r redis-cli del >/dev/null
        done
      fi
      systemctl start $SERVICES
      echo "Restarted: $SERVICES"
    else
      echo "No dispatcharr services found to restart: restart Dispatcharr yourself."
    fi
  fi
fi
