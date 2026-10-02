#!/bin/bash
# The caption worker (fork/subtitles.md §5b.6) on a Linux/LXC (systemd) install: put in only
# when someone asks for it on the Subtitles tab, and taken out again with Dispatch More.
#
#   bash captions.sh watch   STATE SLUG APP   # install.sh: the root watcher for the tab's requests
#   bash captions.sh request STATE SLUG APP   # what the watcher runs: carries out the request
#   bash captions.sh install STATE SLUG APP   # by hand: the same as the tab's Install
#   bash captions.sh remove  STATE SLUG APP   # uninstall.sh, and the tab's Remove
#
# The worker is apps/channels/captions/worker.py, run from a virtualenv of its own
# (/opt/dispatch-more-captions) with faster-whisper -- and NVIDIA's CUDA libraries only when the
# machine has an NVIDIA card -- as `dispatch-more-captions.service`, on 127.0.0.1:9725, as the
# user Dispatcharr runs as. Models go to the models folder Dispatcharr already uses.
# Never on Docker: there the worker is a container of its own (fork/subtitles.md §5b.6).
# SYSTEMD_DIR and VENV exist for the tests.
set -euo pipefail

ACTION="${1:?watch, request, install or remove}"
STATE="${2:?state directory}"
SLUG="${3:-dispatch-more}"
APP="${4:-/opt/dispatcharr}"
UNITS="${SYSTEMD_DIR:-/etc/systemd/system}"
VENV="${VENV:-/opt/$SLUG-captions}"
SERVICE="$SLUG-captions"
PORT="${CAPTIONS_PORT:-9725}"
REQUEST="$STATE/requests/captions"
STATUS="$STATE/captions-status.json"
LOG="$STATE/captions-install.log"

say() {  # the state the tab shows: installing / installed / removing / removed / failed
  local state="$1" step="${2:-}"
  printf '{"state": "%s", "step": "%s", "at": "%s", "gpu": %s}\n' \
    "$state" "${step//\"/\'}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$([ -n "$(gpu)" ] && echo true || echo false)" > "$STATUS"
  chmod 644 "$STATUS" 2>/dev/null || true
  echo "$state: $step"
}

gpu() { command -v nvidia-smi >/dev/null && nvidia-smi -L 2>/dev/null | grep -m1 -i nvidia || true; }

runs_as() {
  local user
  user="$(systemctl show dispatcharr -p User --value 2>/dev/null || true)"
  echo "${user:-root}"
}

models_dir() {
  local dir="${DISPATCHARR_MODELS_DIR:-/data/models}"
  [ -d "$(dirname "$dir")" ] || dir="$STATE/models"
  echo "$dir/captions"
}

watch() {
  cat > "$UNITS/$SERVICE-request.path" <<UNIT
[Unit]
Description=Install or remove the $SLUG caption worker when the Subtitles tab asks

[Path]
PathExists=$REQUEST

[Install]
WantedBy=multi-user.target
UNIT
  cat > "$UNITS/$SERVICE-request.service" <<UNIT
[Unit]
Description=Install or remove the $SLUG caption worker

[Service]
Type=oneshot
ExecStart=/bin/bash $STATE/captions.sh request $STATE $SLUG $APP
UNIT
  systemctl daemon-reload
  systemctl enable --now "$SERVICE-request.path" >/dev/null 2>&1 \
    || echo "Note: the captions watcher could not be started; install the caption worker by hand (captions.sh install)."
  # An update brings a new worker.py: a worker that runs picks it up
  if systemctl is-active --quiet "$SERVICE.service" 2>/dev/null; then
    systemctl restart "$SERVICE.service" || true
  fi
}

install() {
  : > "$LOG"
  chmod 644 "$LOG" 2>/dev/null || true
  local worker="$APP/apps/channels/captions/worker.py" user models
  [ -f "$worker" ] || { say failed "No caption worker in $APP (is Dispatch More installed?)"; return 1; }
  user="$(runs_as)"
  models="$(models_dir)"

  say installing "Making the Python environment"
  if [ ! -x "$VENV/bin/python" ]; then
    if command -v uv >/dev/null; then
      uv venv --quiet --python 3.12 "$VENV" >>"$LOG" 2>&1 || uv venv --quiet "$VENV" >>"$LOG" 2>&1
    else
      python3 -m venv "$VENV" >>"$LOG" 2>&1
    fi
  fi
  [ -x "$VENV/bin/python" ] || { say failed "Could not make a Python environment in $VENV (python3-venv missing?)"; return 1; }
  local pip=("$VENV/bin/python" -m pip install --quiet --upgrade)
  if command -v uv >/dev/null; then pip=(uv pip install --quiet --upgrade --python "$VENV/bin/python"); else
    "$VENV/bin/python" -m pip install --quiet --upgrade pip >>"$LOG" 2>&1 || true
  fi

  say installing "Installing faster-whisper (about 250 MB)"
  "${pip[@]}" faster-whisper >>"$LOG" 2>&1 || { say failed "Could not install faster-whisper (see $LOG)"; return 1; }
  # Translation with Opus-MT (fork/subtitles.md §9 step 4): CTranslate2 came with faster-whisper
  "${pip[@]}" sentencepiece >>"$LOG" 2>&1 || true

  # The worker puts these on its library path itself (worker.cuda_libraries)
  local libs=""
  if [ -n "$(gpu)" ]; then
    say installing "Installing NVIDIA's CUDA libraries for the card (about 1.5 GB)"
    if "${pip[@]}" nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*" >>"$LOG" 2>&1; then
      libs=1
    else
      echo "CUDA libraries could not be installed; the worker uses the CPU" >>"$LOG"
    fi
  fi

  say installing "Starting the worker"
  mkdir -p "$models"
  [ "$user" = root ] || chown -R "$user" "$models" 2>/dev/null || true
  cat > "$UNITS/$SERVICE.service" <<UNIT
[Unit]
Description=$SLUG caption worker: speech to text for the Subtitles tab (fork/subtitles.md)
After=network.target

[Service]
User=$user
ExecStart=$VENV/bin/python $worker --host 127.0.0.1 --port $PORT --models $models
Environment=HF_HUB_DISABLE_TELEMETRY=1
Restart=on-failure
RestartSec=10
Nice=5

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable "$SERVICE.service" >/dev/null 2>&1 || true
  systemctl restart "$SERVICE.service"
  for _ in $(seq 1 30); do
    if command -v curl >/dev/null && curl -fs "http://127.0.0.1:$PORT/status" >/dev/null; then break; fi
    systemctl is-active --quiet "$SERVICE.service" && ! command -v curl >/dev/null && break
    sleep 1
  done
  if systemctl is-active --quiet "$SERVICE.service"; then
    say installed "Running on 127.0.0.1:$PORT$([ -n "$libs" ] && echo ', with the NVIDIA card')"
  else
    say failed "The worker did not start (journalctl -u $SERVICE)"
    return 1
  fi
}

remove() {
  local had=""
  [ -f "$UNITS/$SERVICE.service" ] || [ -d "$VENV" ] && had=1
  systemctl disable --now "$SERVICE.service" >/dev/null 2>&1 || true
  rm -f "$UNITS/$SERVICE.service"
  rm -rf "$VENV"
  rm -rf "$(models_dir)"
  if [ "${1:-}" = all ]; then
    systemctl disable --now "$SERVICE-request.path" >/dev/null 2>&1 || true
    rm -f "$UNITS/$SERVICE-request.path" "$UNITS/$SERVICE-request.service" "$STATUS" "$LOG" "$REQUEST"
  else
    say removed "The caption worker and its models were removed"
  fi
  systemctl daemon-reload || true
  [ -n "$had" ] && echo "Removed the caption worker ($SERVICE) and its models." || true
}

request() {
  [ -f "$REQUEST" ] || return 0
  local wants
  wants="$(sed -n 's/.*"action": *"\([a-z]*\)".*/\1/p' "$REQUEST" | head -1)"
  # Say what happens before the request goes: the tab, looking in between, saw no request and
  # the old "removed" and stopped looking (2026-10-02)
  case "$wants" in
    install) say installing "Starting the installer" ;;
    remove) say removing "Removing the worker" ;;
  esac
  rm -f "$REQUEST"
  case "$wants" in
    install) install || true ;;
    remove) remove ;;
    *) echo "Unknown caption request: $wants" ;;
  esac
}

case "$ACTION" in
  watch) watch ;;
  request) request ;;
  install) install ;;
  remove) remove "${5:-}" ;;
  *) echo "Unknown action: $ACTION"; exit 2 ;;
esac
