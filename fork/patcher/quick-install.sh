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
set -euo pipefail

REPOSITORY="${DISPATCH_MORE_REPOSITORY:-ckegels/dispatch-more}"
APP="${DISPATCHARR_APP:-}"
if [ -f /.dockerenv ] || grep -qs docker /proc/1/cgroup; then
  LAYOUT=docker; APP="${APP:-/app}"; STATE="${STATE:-/data/dispatch-more}"
else
  LAYOUT=systemd; APP="${APP:-/opt/dispatcharr}"; STATE="${STATE:-/var/lib/dispatch-more}"
fi
[ -f "$APP/version.py" ] || { echo "No Dispatcharr found at $APP (set DISPATCHARR_APP to where it is)."; exit 1; }
VERSION="$(sed -n "s/^__version__ = '\(.*\)'.*/\1/p" "$APP/version.py")"
echo "Dispatcharr $VERSION found at $APP ($LAYOUT)."

PY=""
for candidate in "$APP/.venv/bin/python" "$APP/env/bin/python" "$(command -v python3 || true)"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then PY="$candidate"; break; fi
done
[ -n "$PY" ] || { echo "No Python found."; exit 1; }

# Fetching with whatever is there: curl, wget, or Dispatcharr's own Python
fetch() {
  if command -v curl >/dev/null; then curl -fsSL "$1" -o "$2"
  elif command -v wget >/dev/null; then wget -qO "$2" "$1"
  else "$PY" -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" "$1" "$2"
  fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ARCHIVE="${DISPATCH_MORE_ARCHIVE:-}"
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
  [ -n "$ARCHIVE" ] || { echo "There is no Dispatch More release for Dispatcharr $VERSION yet. Nothing was changed."; exit 3; }
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
