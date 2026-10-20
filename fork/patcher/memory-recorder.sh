#!/bin/bash
# The memory recorder (apps/proxy/live_proxy/memory_recorder.py) as a process of its own, so it
# keeps writing while the web workers are stuck; read back on Diagnostics -> Memory -> History.
#
#   bash memory-recorder.sh add    STATE SLUG APP   # install.sh, Linux/LXC (systemd)
#   bash memory-recorder.sh remove STATE SLUG       # uninstall.sh
#   bash memory-recorder.sh docker STATE APP        # docker-entrypoint.sh: started in the background
#
# The history is in /data/memory-history (MEMORY_HISTORY_DIR), which survives a reboot and, on
# Docker, a new container: it is never removed here, uninstalling included. The folder belongs to
# the user the web app runs as, so the tab can switch the recorder (the flag file `on`) and read
# what it wrote. The recorder itself only records while that flag exists.
# SYSTEMD_DIR exists for the tests; on a server it is /etc/systemd/system.
set -euo pipefail
ACTION="${1:?add, remove or docker}"
STATE="${2:?state directory}"
UNITS="${SYSTEMD_DIR:-/etc/systemd/system}"
DIR="${MEMORY_HISTORY_DIR:-/data/memory-history}"

python_of() {
  local app="$1" candidate
  for candidate in "$app/.venv/bin/python" "$app/env/bin/python" "$(command -v python3 || true)"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then echo "$candidate"; return; fi
  done
}

own() {  # the history folder, for the user the web app runs as
  mkdir -p "$DIR"
  chown "$1" "$DIR" 2>/dev/null || true
  chmod 755 "$DIR" 2>/dev/null || true
}

add() {
  local slug="${3:-dispatch-more}" app="${4:-/opt/dispatcharr}" py user
  local service="$slug-memory"
  py="$(python_of "$app")"
  [ -n "$py" ] || { echo "Note: no Python found; the memory recorder was not added."; return 0; }
  user="$(systemctl show dispatcharr -p User --value 2>/dev/null || true)"
  user="${user:-root}"
  own "$user"
  cat > "$UNITS/$service.service" <<UNIT
[Unit]
Description=$slug memory recorder (Diagnostics > Memory > History)
After=network.target

[Service]
Type=simple
User=$user
Environment=MEMORY_HISTORY_DIR=$DIR
ExecStart=$py $app/apps/proxy/live_proxy/memory_recorder.py --dir $DIR
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload || true
  systemctl enable "$service.service" >/dev/null 2>&1 || true
  systemctl restart "$service.service" >/dev/null 2>&1 || true
  echo "$service" > "$STATE/memory-recorder"
  echo "Added the memory recorder ($service); switch it on at Diagnostics > Memory > History."
}

remove() {
  local slug="${3:-dispatch-more}" service
  service="$(cat "$STATE/memory-recorder" 2>/dev/null || echo "$slug-memory")"
  [ -f "$UNITS/$service.service" ] || { rm -f "$STATE/memory-recorder"; return 0; }
  systemctl disable --now "$service.service" >/dev/null 2>&1 || true
  rm -f "$UNITS/$service.service" "$STATE/memory-recorder"
  systemctl daemon-reload || true
  echo "Removed the memory recorder ($service); its history stays in $DIR."
}

docker() {
  local app="${3:-/app}" py owner
  py="$(python_of "$app")"
  [ -n "$py" ] || { echo "Note: no Python found; the memory recorder was not started."; return 0; }
  owner="${PUID:-$(id -u "${POSTGRES_USER:-dispatch}" 2>/dev/null || echo 0)}"
  own "$owner"
  pkill -f "memory_recorder.py --dir" 2>/dev/null || true
  # Restarted if it ever stops; detached so it outlives this script's exec into Dispatcharr
  setsid bash -c "while true; do MEMORY_HISTORY_DIR='$DIR' '$py' '$app/apps/proxy/live_proxy/memory_recorder.py' --dir '$DIR'; sleep 5; done" \
    </dev/null >/dev/null 2>&1 &
  echo "Started the memory recorder; switch it on at Diagnostics > Memory > History."
}

case "$ACTION" in
  add) add "$@" ;;
  remove) remove "$@" ;;
  docker) docker "$@" ;;
  *) echo "Unknown action: $ACTION"; exit 2 ;;
esac
