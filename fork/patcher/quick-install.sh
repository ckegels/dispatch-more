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
#
# Dispatcharr and Dispatch More updated together (Linux / LXC):
#
#   curl -fsSL https://github.com/ckegels/dispatch-more/releases/latest/download/quick-install.sh | sudo bash -s -- --upgrade
#
# Dispatcharr's own updaters (debian_install.sh, the Proxmox script's `update`) always install
# the newest Dispatcharr, which may have no Dispatch More release yet. --upgrade updates it to
# the version the newest Dispatch More release is made for, never past it: Dispatch More off
# (Dispatcharr is stock while it is updated), Dispatcharr updated the way it was installed,
# Dispatch More on again. Dispatcharr already there: the same as without --upgrade.
#
# --fetch-only (Docker, used by docker-entrypoint.sh): the release for this Dispatcharr is
# downloaded into the state folder and nothing is installed; the entrypoint applies it.
set -euo pipefail

REPOSITORY="${DISPATCH_MORE_REPOSITORY:-ckegels/dispatch-more}"
APP="${DISPATCHARR_APP:-}"
# DISPATCH_MORE_LAYOUT=docker|systemd says which, for a caller that knows (docker-entrypoint.sh)
if [ "${DISPATCH_MORE_LAYOUT:-}" = docker ] || { [ -z "${DISPATCH_MORE_LAYOUT:-}" ] && { [ -f /.dockerenv ] || grep -qs docker /proc/1/cgroup; }; }; then
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

# Fetching with whatever is there: curl, wget, or Python. DISPATCH_MORE_FETCH_TIMEOUT (seconds)
# bounds each download: a container start never waits long for GitHub.
fetch() {
  local limit="${DISPATCH_MORE_FETCH_TIMEOUT:-}"
  if command -v curl >/dev/null; then curl -fsSL ${limit:+--max-time "$limit"} "$1" -o "$2"
  elif command -v wget >/dev/null; then wget -q ${limit:+--timeout="$limit"} -O "$2" "$1"
  elif [ -n "$PY" ]; then "$PY" -c "import sys, urllib.request; open(sys.argv[2], 'wb').write(urllib.request.urlopen(sys.argv[1], timeout=float(sys.argv[3] or 60)).read())" "$1" "$2" "$limit"
  else echo "Neither curl, wget nor Python is here to download with."; exit 1
  fi
}
# A release archive, from a URL or a file, to $2
get_archive() {
  case "$1" in
    http*) fetch "$1" "$2" ;;
    *) cp "$1" "$2" ;;
  esac
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ARCHIVE="${DISPATCH_MORE_ARCHIVE:-}"
ACTION=install
for arg in "$@"; do
  case "$arg" in
    --uninstall) ACTION=uninstall ;;
    --upgrade) ACTION=upgrade ;;
    --fetch-only) ACTION=fetch ;;
    *) echo "Unknown option: $arg"; exit 2 ;;
  esac
done

# The published releases, newest first (fetched once)
releases_json() {
  [ -f "$WORK/releases.json" ] || fetch "https://api.github.com/repos/$REPOSITORY/releases?per_page=30" "$WORK/releases.json"
}
# The newest release's archive for this Dispatcharr version ($1), or nothing
release_for() {
  releases_json
  "$PY" - "$WORK/releases.json" "$1" <<'PY'
import json, sys
releases, version = json.load(open(sys.argv[1])), sys.argv[2]
for release in releases:
    for asset in release.get("assets", []):
        if asset["name"].endswith(f"-dispatcharr-{version}.tar.gz"):
            print(asset["browser_download_url"]); sys.exit(0)
PY
}
# The newest release's archive, whatever Dispatcharr it is for, or nothing
newest_release() {
  releases_json
  "$PY" - "$WORK/releases.json" <<'PY'
import json, sys
for release in json.load(open(sys.argv[1])):
    for asset in release.get("assets", []):
        if asset["name"].startswith("dispatch-more-v") and asset["name"].endswith(".tar.gz"):
            print(asset["browser_download_url"]); sys.exit(0)
PY
}
# The Dispatcharr version an archive is made for: a file says so in its manifest (its name may be
# anything -- a release saved as release.tar.gz said nothing, and the newest release's version
# was taken instead), a URL in its name
dispatcharr_of() {
  local found=""
  if [ -f "$1" ]; then
    found="$(tar -xzOf "$1" dispatch-more/manifest.json 2>/dev/null | sed -n 's/.*"for_dispatcharr": *"\([^"]*\)".*/\1/p' | head -1)"
  fi
  [ -n "$found" ] || found="$(echo "${1##*/}" | sed -n 's/.*-dispatcharr-\(.*\)\.tar\.gz$/\1/p')"
  echo "$found"
}
version_here() { sed -n "s/^__version__ = '\(.*\)'.*/\1/p" "$APP/version.py"; }

# Dispatch More taken off with the scripts of the archive in $1 (the newest), never the ones the
# installed release left: those put their own Dispatcharr version's files back even over a newer
# Dispatcharr (before v276)
uninstall_with() {
  mkdir -p "$WORK/new"
  tar -xzf "$1" -C "$WORK/new" --strip-components=1
  # The scripts, never the manifest: that one says what the installed release put where
  for script in patch.py uninstall.sh install.sh docker-entrypoint.sh dvr-worker.sh captions.sh memory-recorder.sh quick-install.sh; do
    if [ -f "$WORK/new/$script" ]; then cp "$WORK/new/$script" "$STATE/$script"; fi
  done
  if [ "$LAYOUT" = docker ]; then
    STATE="$STATE" bash "$STATE/uninstall.sh" --docker --app "$APP" ${DISPATCH_MORE_INSTALL_ARGS:-}
  else
    STATE="$STATE" bash "$STATE/uninstall.sh" --app "$APP" ${DISPATCH_MORE_INSTALL_ARGS:-}
  fi
}

# Dispatcharr's own installer (debian_install.sh) for version $1: a new install, or an update
# of one it made. Its "type I understand" question is the user's to answer.
run_debian_install() {
  local want="$1"
  fetch "https://raw.githubusercontent.com/Dispatcharr/Dispatcharr/v$want/debian_install.sh" "$WORK/debian_install.sh"
  # Its own default is the newest Dispatcharr ("main"); pinned to the release's version
  grep -q '^ *DISPATCH_BRANCH="main"' "$WORK/debian_install.sh" || {
    echo "Dispatcharr's installer has changed (no DISPATCH_BRANCH to pin); install Dispatcharr $want yourself first."
    exit 3
  }
  sed -i "s|^\( *\)DISPATCH_BRANCH=\"main\"|\1DISPATCH_BRANCH=\"v$want\"|" "$WORK/debian_install.sh"
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
}

# Which installer made this Dispatcharr: debian_install.sh (a git checkout with env/) or the
# Proxmox community script (a release tarball with .venv/, start-uwsgi.sh and uv). Anything
# else is updated by whoever made it.
installed_by() {
  if [ -d "$APP/.git" ] && [ -x "$APP/env/bin/python" ]; then echo debian_install
  elif [ -x "$APP/.venv/bin/python" ] && [ -f "$APP/start-uwsgi.sh" ] && command -v uv >/dev/null; then echo community
  else echo unknown
  fi
}

SERVICES="dispatcharr dispatcharr-celery dispatcharr-celerybeat dispatcharr-daphne"

# The Proxmox community script's own update (ct/dispatcharr.sh, update_script), step by step,
# pinned to version $1 -- its `update` always takes the newest Dispatcharr. The same backup
# (/opt/dispatcharr_backup_<time>.tar.gz with a database dump), the same clean install keeping
# .env and the start scripts, the same build, and the installed version where its `update`
# reads it ($HOME/.dispatcharr).
community_update() {
  local want="$1" stamp keep backup
  stamp="$(date +%F_%H-%M-%S)"
  backup="/opt/dispatcharr_backup_$stamp.tar.gz"
  echo "Stopping Dispatcharr"
  systemctl stop dispatcharr-celery dispatcharr-celerybeat dispatcharr-daphne dispatcharr
  echo "Backing up $APP and its database to $backup"
  if [ -f "$APP/.env" ]; then
    (
      set -a; . "$APP/.env"; set +a
      if [ -n "${POSTGRES_DB:-}" ] && [ -n "${POSTGRES_USER:-}" ] && [ -n "${POSTGRES_PASSWORD:-}" ]; then
        PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -h "${POSTGRES_HOST:-localhost}" \
          -p "${POSTGRES_PORT:-5432}" "$POSTGRES_DB" >"$WORK/dispatcharr_db_$stamp.sql"
      fi
    )
  fi
  tar -czf "$backup" -C /opt "$(basename "$APP")" $( [ -f "$WORK/dispatcharr_db_$stamp.sql" ] && echo -C "$WORK" "dispatcharr_db_$stamp.sql" )
  echo "Getting Dispatcharr $want"
  fetch "https://github.com/Dispatcharr/Dispatcharr/archive/refs/tags/v$want.tar.gz" "$WORK/dispatcharr.tar.gz"
  keep="$WORK/keep"; mkdir -p "$keep"
  for file in .env start-uwsgi.sh start-celery.sh start-celerybeat.sh start-daphne.sh; do
    [ -f "$APP/$file" ] && cp -p "$APP/$file" "$keep/"
  done
  find "$APP" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  tar --no-same-owner -xzf "$WORK/dispatcharr.tar.gz" -C "$APP" --strip-components=1
  cp -rp "$keep"/. "$APP"/
  echo "Installing Dispatcharr $want's Python packages"
  (
    cd "$APP"
    rm -rf .venv
    uv venv --clear -q
    uv sync -q
    uv pip install -q uwsgi gevent celery redis daphne
  )
  echo "Building Dispatcharr $want's web page"
  (
    cd "$APP/frontend"
    node -e "const p=require('./package.json');p.overrides=p.overrides||{};p.overrides['webworkify-webpack']='2.1.3';require('fs').writeFileSync('package.json',JSON.stringify(p,null,2));"
    rm -f package-lock.json
    npm install --no-audit --progress=false --loglevel=error
    npm run build --silent
  )
  echo "Migrating the database"
  (
    cd "$APP"
    set -a; [ -f .env ] && . ./.env; set +a
    uv run python manage.py migrate --noinput
    uv run python manage.py collectstatic --noinput >/dev/null
  )
  # Where the community script's `update` reads the installed version, in the form it wrote
  if [ -f "$HOME/.dispatcharr" ] && grep -q '^v' "$HOME/.dispatcharr"; then
    echo "v$want" >"$HOME/.dispatcharr"
  else
    echo "$want" >"$HOME/.dispatcharr"
  fi
  echo "Starting Dispatcharr"
  systemctl start $SERVICES
  echo "Dispatcharr $want is installed (backup of the old one: $backup)."
}

# Every Dispatcharr service answering again after an update: uWSGI restarted, so the new
# version answers (debian_install.sh leaves running services running), and given time to start
restart_dispatcharr() {
  systemctl restart $SERVICES
  for _ in $(seq 1 60); do
    curl -fsS localhost:9191/api/core/version/ >/dev/null 2>&1 && return 0
    sleep 2
  done
}

if [ "$ACTION" = uninstall ]; then
  [ -f "$STATE/manifest.json" ] || { echo "Dispatch More is not installed here (nothing in $STATE)."; exit 0; }
  [ -n "$PY" ] || { echo "No Python found."; exit 1; }
  if [ -z "$ARCHIVE" ]; then
    # Any release will do: only its scripts are used, whatever Dispatcharr version it is for
    ARCHIVE="$(newest_release)"
    [ -n "$ARCHIVE" ] || { echo "Could not find a Dispatch More release to uninstall with. Nothing was changed."; exit 3; }
  fi
  echo "Getting the uninstaller from $ARCHIVE"
  get_archive "$ARCHIVE" "$WORK/release.tar.gz"
  uninstall_with "$WORK/release.tar.gz"
  if [ "$LAYOUT" = docker ]; then
    # The start script (if the compose file uses it) stays stock from now on
    touch "$STATE/uninstalled"
    echo "Uninstalled. Restart the container (docker restart <container name>) to run stock Dispatcharr."
  fi
  exit 0
fi

if [ "$ACTION" = upgrade ] && [ "$LAYOUT" = docker ]; then
  echo "In Docker, Dispatcharr is updated by pulling its new image and recreating the container."
  echo "With Dispatch More's start script in the compose file, the matching release is then put on by"
  echo "itself (when there is one); without it, run the install command in the new container."
  exit 2
fi

if [ "$ACTION" = upgrade ] && [ -f "$APP/version.py" ]; then
  [ -n "$PY" ] || { echo "No Python found."; exit 1; }
  if [ -z "$ARCHIVE" ]; then
    ARCHIVE="$(newest_release)"
    [ -n "$ARCHIVE" ] || { echo "Could not find a Dispatch More release. Nothing was changed."; exit 3; }
  fi
  WANT="$(dispatcharr_of "$ARCHIVE")"
  HAVE="$(version_here)"
  [ -n "$WANT" ] || { echo "Could not tell which Dispatcharr version the release is for. Nothing was changed."; exit 3; }
  if [ "$HAVE" = "$WANT" ]; then
    echo "Dispatcharr is $HAVE already, the version the newest Dispatch More is made for."
  elif [ "$(printf '%s\n%s\n' "$HAVE" "$WANT" | sort -V | tail -1)" = "$HAVE" ]; then
    echo "Dispatcharr $HAVE is newer than any Dispatch More release (the newest is for $WANT)."
    echo "Nothing was changed; Dispatcharr is not taken back to an older version."
    exit 3
  else
    [ "$(id -u)" = 0 ] || { echo "Run as root: Dispatcharr's files and services are updated. Nothing was changed."; exit 1; }
    BY="$(installed_by)"
    if [ "$BY" = unknown ]; then
      echo "Dispatcharr $HAVE here was not installed by debian_install.sh or the Proxmox community script,"
      echo "so it is not updated from here. Update it to Dispatcharr $WANT (not newer) the way you installed"
      echo "it, then run the install command again. Nothing was changed."
      exit 3
    fi
    echo "Dispatcharr $HAVE -> $WANT (the version the newest Dispatch More is made for; installed by $BY),"
    echo "then Dispatch More on it. Dispatch More is taken off first, so Dispatcharr is stock while it"
    echo "is updated. Starting in ${DISPATCH_MORE_WAIT:-10} seconds; Ctrl-C to stop."
    sleep "${DISPATCH_MORE_WAIT:-10}"
    get_archive "$ARCHIVE" "$WORK/release.tar.gz"
    if [ -f "$STATE/manifest.json" ]; then
      uninstall_with "$WORK/release.tar.gz"
    fi
    if [ "$BY" = debian_install ]; then
      run_debian_install "$WANT"
      restart_dispatcharr
    else
      community_update "$WANT"
    fi
    find_python
    HAVE="$(version_here)"
    if [ "$HAVE" != "$WANT" ]; then
      echo "Dispatcharr's update did not end on $WANT (it is $HAVE); Dispatch More was not put back on."
      exit 1
    fi
    ARCHIVE="$WORK/release.tar.gz"
  fi
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
  WANT="$(dispatcharr_of "$ARCHIVE")"
  if [ -z "$WANT" ]; then
    find_python
    if [ -n "$PY" ]; then
      WANT="$(dispatcharr_of "$(newest_release)")"
    else
      fetch "https://api.github.com/repos/$REPOSITORY/releases?per_page=30" "$WORK/releases.json"
      WANT="$(grep -o 'dispatch-more-v[0-9]*-dispatcharr-[0-9][0-9.]*[0-9]\.tar\.gz' "$WORK/releases.json" | head -1 | sed 's/.*-dispatcharr-\(.*\)\.tar\.gz/\1/')"
    fi
  fi
  [ -n "$WANT" ] || { echo "Could not tell which Dispatcharr version the release is for. Nothing was changed."; exit 3; }
  echo "No Dispatcharr found at $APP."
  echo "Dispatcharr $WANT is installed first, with Dispatcharr's own installer (debian_install.sh from"
  echo "github.com/Dispatcharr/Dispatcharr, version $WANT: the one this release is made for), then"
  echo "Dispatch More over it. Starting in ${DISPATCH_MORE_WAIT:-10} seconds; Ctrl-C to stop."
  sleep "${DISPATCH_MORE_WAIT:-10}"
  run_debian_install "$WANT"
  [ -f "$APP/version.py" ] || { echo "Dispatcharr's installer did not put Dispatcharr at $APP; Dispatch More was not installed."; exit 1; }
  find_python
fi
VERSION="$(version_here)"
echo "Dispatcharr $VERSION found at $APP ($LAYOUT)."
[ -n "$PY" ] || { echo "No Python found."; exit 1; }
if [ -n "$ARCHIVE" ] && [ "$ACTION" = fetch ]; then
  # Given on purpose (tests, or a release kept locally): only when it is for this Dispatcharr
  [ "$(dispatcharr_of "$ARCHIVE")" = "$VERSION" ] || ARCHIVE=""
fi
if [ -z "$ARCHIVE" ]; then
  # The newest release that has a build for this Dispatcharr version
  ARCHIVE="$(release_for "$VERSION")"
  if [ -z "$ARCHIVE" ]; then
    echo "There is no Dispatch More release for Dispatcharr $VERSION yet. Nothing was changed."
    # Dispatcharr updated under an installed release: what that release left (its added files,
    # its services) is still there, and is taken off with --uninstall
    if [ -f "$STATE/record.json" ] && [ "$ACTION" != fetch ]; then
      echo "Dispatch More is still recorded as installed here ($STATE). To take what is left of it off:"
      echo "  curl -fsSL https://github.com/$REPOSITORY/releases/latest/download/quick-install.sh | sudo bash -s -- --uninstall"
    fi
    exit 3
  fi
fi
if [ "$ARCHIVE" != "$WORK/release.tar.gz" ]; then
  echo "Getting $ARCHIVE"
  get_archive "$ARCHIVE" "$WORK/release.tar.gz"
fi

if [ "$LAYOUT" = docker ]; then
  # Unpacked where it stays: in the /data volume, so the entrypoint can apply it at every start
  mkdir -p "$STATE"
  tar -xzf "$WORK/release.tar.gz" -C "$STATE" --strip-components=1
  if [ "$ACTION" = fetch ]; then
    echo "The release for Dispatcharr $VERSION is in $STATE."
    exit 0
  fi
  STATE="$STATE" DISPATCH_MORE_VIA=exec bash "$STATE/install.sh" --docker --app "$APP" ${DISPATCH_MORE_INSTALL_ARGS:-}
  cat <<EOF

Installed. Now restart the container, from the host:

    docker restart <container name>

A restart keeps it. Recreating the container (a new image, or a changed compose file) starts
stock Dispatcharr again: run this command again then. Or have it put back at every start by
adding this to the container in docker-compose.yml (then a newer Dispatcharr image gets the
release made for it by itself, when there is one):

    entrypoint: ["/bin/bash", "$STATE/docker-entrypoint.sh"]

To uninstall: recreate the container (docker compose up -d --force-recreate) without that line.
EOF
else
  [ "$ACTION" = fetch ] && { echo "--fetch-only is for Docker's start script."; exit 2; }
  tar -xzf "$WORK/release.tar.gz" -C "$WORK"
  STATE="$STATE" bash "$WORK/dispatch-more/install.sh" --app "$APP" ${DISPATCH_MORE_INSTALL_ARGS:-}
fi
