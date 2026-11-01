#!/bin/bash
# Dispatch More in one command: finds which Dispatcharr this is, downloads the release made for
# it from GitHub, and installs it. The same on Linux or an LXC and inside a Docker container.
#
#   Linux / LXC:  curl -fsSL https://github.com/ckegels/dispatch-more/releases/latest/download/quick-install.sh | sudo bash
#   Docker:       docker exec dispatcharr bash -c "curl -fsSL https://github.com/ckegels/dispatch-more/releases/latest/download/quick-install.sh | bash" && docker restart dispatcharr
#
# Not official Dispatcharr. To go back: Settings -> System -> Modified build.
#
# DISPATCH_MORE_ARCHIVE=<file or URL> installs that release instead of looking one up;
# DISPATCH_MORE_INSTALL_ARGS is passed on to install.sh (e.g. --force).
#
# Taking it off again, also after Dispatcharr was updated under it:
#
#   curl -fsSL https://github.com/ckegels/dispatch-more/releases/latest/download/quick-install.sh | sudo bash -s -- --uninstall
#
# This uses the newest release's uninstaller, not the one the installed release left behind:
# releases before v276 put their own Dispatcharr version's files back even over a newer
# Dispatcharr.
#
# No Dispatcharr yet (a new Linux machine or LXC): Dispatcharr's own installer
# (debian_install.sh) is run first, pinned to the Dispatcharr version the release is made for --
# its own default is the newest Dispatcharr, which a release for another version refuses -- then
# Dispatch More goes over it. The same one command for a new server and an update.
set -euo pipefail

REPOSITORY="${DISPATCH_MORE_REPOSITORY:-ckegels/dispatch-more}"
APP="${DISPATCHARR_APP:-}"
if [ -f /.dockerenv ] || grep -qs docker /proc/1/cgroup; then
  LAYOUT=docker; APP="${APP:-/app}"; STATE="${STATE:-/data/dispatch-more}"
else
  LAYOUT=systemd; APP="${APP:-/opt/dispatcharr}"; STATE="${STATE:-/var/lib/dispatch-more}"
fi
find_python() {
  PY=""
  for candidate in "$APP/.venv/bin/python" "$APP/env/bin/python" "$(command -v python3 || true)"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then PY="$candidate"; return; fi
  done
}
find_python

# Fetching with whatever is there: curl, wget, or Python
fetch() {
  if command -v curl >/dev/null; then curl -fsSL "$1" -o "$2"
  elif command -v wget >/dev/null; then wget -qO "$2" "$1"
  elif [ -n "$PY" ]; then "$PY" -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" "$1" "$2"
  else echo "Neither curl, wget nor Python is here to download with."; exit 1
  fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ARCHIVE="${DISPATCH_MORE_ARCHIVE:-}"
ACTION=install
for arg in "$@"; do
  case "$arg" in
    --uninstall) ACTION=uninstall ;;
    *) echo "Unknown option: $arg"; exit 2 ;;
  esac
done

if [ "$ACTION" = uninstall ]; then
  [ -f "$STATE/manifest.json" ] || { echo "Dispatch More is not installed here (nothing in $STATE)."; exit 0; }
  [ -n "$PY" ] || { echo "No Python found."; exit 1; }
  if [ -z "$ARCHIVE" ]; then
    # Any release will do: only its scripts are used, whatever Dispatcharr version it is for
    fetch "https://api.github.com/repos/$REPOSITORY/releases?per_page=30" "$WORK/releases.json"
    ARCHIVE="$("$PY" - "$WORK/releases.json" <<'PY'
import json, sys
for release in json.load(open(sys.argv[1])):
    for asset in release.get("assets", []):
        if asset["name"].startswith("dispatch-more-v") and asset["name"].endswith(".tar.gz"):
            print(asset["browser_download_url"]); sys.exit(0)
PY
)"
    [ -n "$ARCHIVE" ] || { echo "Could not find a Dispatch More release to uninstall with. Nothing was changed."; exit 3; }
  fi
  echo "Getting the uninstaller from $ARCHIVE"
  case "$ARCHIVE" in
    http*) fetch "$ARCHIVE" "$WORK/release.tar.gz" ;;
    *) cp "$ARCHIVE" "$WORK/release.tar.gz" ;;
  esac
  mkdir -p "$WORK/new"
  tar -xzf "$WORK/release.tar.gz" -C "$WORK/new" --strip-components=1
  # The scripts, never the manifest: that one says what the installed release put where
  for script in patch.py uninstall.sh install.sh docker-entrypoint.sh dvr-worker.sh captions.sh memory-recorder.sh; do
    if [ -f "$WORK/new/$script" ]; then cp "$WORK/new/$script" "$STATE/$script"; fi
  done
  if [ "$LAYOUT" = docker ]; then
    STATE="$STATE" bash "$STATE/uninstall.sh" --docker --app "$APP" ${DISPATCH_MORE_INSTALL_ARGS:-}
    # The start script (if the compose file uses it) stays stock from now on
    touch "$STATE/uninstalled"
    echo "Uninstalled. Restart the container (docker restart <container name>) to run stock Dispatcharr."
  else
    STATE="$STATE" bash "$STATE/uninstall.sh" --app "$APP" ${DISPATCH_MORE_INSTALL_ARGS:-}
  fi
  exit 0
fi

if [ ! -f "$APP/version.py" ]; then
  if [ "$LAYOUT" = docker ]; then
    echo "No Dispatcharr found at $APP. In Docker, Dispatcharr comes from its image: start the official"
    echo "Dispatcharr image, then run this inside it (see the README, Install)."
    exit 1
  fi
  if [ "$(id -u)" != 0 ] || ! command -v apt-get >/dev/null || ! command -v systemctl >/dev/null; then
    echo "No Dispatcharr found at $APP (set DISPATCHARR_APP to where it is)."
    echo "To have it installed here too, run this as root on Debian or Ubuntu with systemd."
    exit 1
  fi
  # The Dispatcharr version the release is made for: from the archive's name, or the newest release
  WANT="$(echo "${ARCHIVE##*/}" | sed -n 's/.*-dispatcharr-\(.*\)\.tar\.gz$/\1/p')"
  if [ -z "$WANT" ]; then
    fetch "https://api.github.com/repos/$REPOSITORY/releases?per_page=30" "$WORK/releases.json"
    WANT="$(grep -o 'dispatch-more-v[0-9]*-dispatcharr-[0-9][0-9.]*[0-9]\.tar\.gz' "$WORK/releases.json" | head -1 | sed 's/.*-dispatcharr-\(.*\)\.tar\.gz/\1/')"
  fi
  [ -n "$WANT" ] || { echo "Could not tell which Dispatcharr version the release is for. Nothing was changed."; exit 3; }
  echo "No Dispatcharr found at $APP."
  echo "Dispatcharr $WANT is installed first, with Dispatcharr's own installer (debian_install.sh from"
  echo "github.com/Dispatcharr/Dispatcharr, version $WANT: the one this release is made for), then"
  echo "Dispatch More over it. Starting in ${DISPATCH_MORE_WAIT:-10} seconds; Ctrl-C to stop."
  sleep "${DISPATCH_MORE_WAIT:-10}"
  fetch "https://raw.githubusercontent.com/Dispatcharr/Dispatcharr/v$WANT/debian_install.sh" "$WORK/debian_install.sh"
  # Its own default is the newest Dispatcharr ("main"); pinned to the release's version
  grep -q '^ *DISPATCH_BRANCH="main"' "$WORK/debian_install.sh" || {
    echo "Dispatcharr's installer has changed (no DISPATCH_BRANCH to pin); install Dispatcharr $WANT yourself first."
    exit 3
  }
  sed -i "s|^\( *\)DISPATCH_BRANCH=\"main\"|\1DISPATCH_BRANCH=\"v$WANT\"|" "$WORK/debian_install.sh"
  # Its first step asks you to type "I understand" under its own warning. That answer is yours:
  # asked on the terminal (stdin here is this script itself when piped from curl, which its
  # `read` would swallow), or given on purpose for an unattended install.
  if [ -n "${DISPATCH_MORE_DISPATCHARR_ANSWER:-}" ]; then
    printf '%s\n' "$DISPATCH_MORE_DISPATCHARR_ANSWER" | bash "$WORK/debian_install.sh"
  elif ( : </dev/tty ) 2>/dev/null; then
    echo "Dispatcharr's installer asks you to confirm its warning: type what it asks for."
    bash "$WORK/debian_install.sh" </dev/tty
  else
    echo "Dispatcharr's installer asks you to type \"I understand\" under its warning, and there is no"
    echo "terminal here to ask in. Run this in a terminal, or set DISPATCH_MORE_DISPATCHARR_ANSWER=\"I understand\"."
    exit 3
  fi
  [ -f "$APP/version.py" ] || { echo "Dispatcharr's installer did not put Dispatcharr at $APP; Dispatch More was not installed."; exit 1; }
  find_python
fi
VERSION="$(sed -n "s/^__version__ = '\(.*\)'.*/\1/p" "$APP/version.py")"
echo "Dispatcharr $VERSION found at $APP ($LAYOUT)."
[ -n "$PY" ] || { echo "No Python found."; exit 1; }
if [ -z "$ARCHIVE" ]; then
  # The newest release that has a build for this Dispatcharr version
  fetch "https://api.github.com/repos/$REPOSITORY/releases?per_page=30" "$WORK/releases.json"
  ARCHIVE="$("$PY" - "$WORK/releases.json" "$VERSION" <<'PY'
import json, sys
releases, version = json.load(open(sys.argv[1])), sys.argv[2]
for release in releases:
    for asset in release.get("assets", []):
        if asset["name"].endswith(f"-dispatcharr-{version}.tar.gz"):
            print(asset["browser_download_url"]); sys.exit(0)
PY
)"
  if [ -z "$ARCHIVE" ]; then
    echo "There is no Dispatch More release for Dispatcharr $VERSION yet. Nothing was changed."
    # Dispatcharr updated under an installed release: what that release left (its added files,
    # its services) is still there, and is taken off with --uninstall
    if [ -f "$STATE/record.json" ]; then
      echo "Dispatch More is still recorded as installed here ($STATE). To take what is left of it off:"
      echo "  curl -fsSL https://github.com/$REPOSITORY/releases/latest/download/quick-install.sh | sudo bash -s -- --uninstall"
    fi
    exit 3
  fi
fi
echo "Getting $ARCHIVE"
case "$ARCHIVE" in
  http*) fetch "$ARCHIVE" "$WORK/release.tar.gz" ;;
  *) cp "$ARCHIVE" "$WORK/release.tar.gz" ;;
esac

if [ "$LAYOUT" = docker ]; then
  # Unpacked where it stays: in the /data volume, so the entrypoint can apply it at every start
  mkdir -p "$STATE"
  tar -xzf "$WORK/release.tar.gz" -C "$STATE" --strip-components=1
  STATE="$STATE" DISPATCH_MORE_VIA=exec bash "$STATE/install.sh" --docker --app "$APP" ${DISPATCH_MORE_INSTALL_ARGS:-}
  cat <<EOF

Installed. Now restart the container, from the host:

    docker restart <container name>

A restart keeps it. Recreating the container (a new image, or a changed compose file) starts
stock Dispatcharr again: run this command again then. Or have it put back at every start by
adding this to the container in docker-compose.yml:

    entrypoint: ["/bin/bash", "$STATE/docker-entrypoint.sh"]

To uninstall: recreate the container (docker compose up -d --force-recreate) without that line.
EOF
else
  tar -xzf "$WORK/release.tar.gz" -C "$WORK"
  STATE="$STATE" bash "$WORK/dispatch-more/install.sh" --app "$APP" ${DISPATCH_MORE_INSTALL_ARGS:-}
fi
