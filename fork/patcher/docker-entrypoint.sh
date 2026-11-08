#!/bin/bash
# Docker: puts Dispatch More over the official image every time the container starts, then
# starts Dispatcharr as the image would. Point the container at it in docker-compose.yml:
#
#   entrypoint: ["/bin/bash", "/data/dispatch-more/docker-entrypoint.sh"]
#
# (a separate celery container: add  environment: DISPATCHARR_ENTRYPOINT=/app/docker/entrypoint.celery.sh)
#
# A newer Dispatcharr image (pulled, the container recreated) gets the Dispatch More release made
# for it by itself: the one in /data is for the old version, so the matching one is fetched from
# GitHub first (quick-install.sh --fetch-only, at most DISPATCH_MORE_FETCH_TIMEOUT seconds per
# download, 60 by default). No release for that version yet, or no internet: the release refuses
# the image, the log says so, and Dispatcharr starts stock. DISPATCH_MORE_AUTO_UPDATE=0 leaves
# the release in /data as it is.
#
# After the page's Uninstall button, the next start puts stock back and stays stock (nothing is
# fetched either); delete /data/dispatch-more/uninstalled to have it installed again.
STATE="$(cd "$(dirname "$0")" && pwd)"
APP="${DISPATCHARR_APP:-/app}"
ORIGINAL="${DISPATCHARR_ENTRYPOINT:-$APP/docker/entrypoint.sh}"

if [ -f "$STATE/requests/uninstall" ]; then
  STATE="$STATE" bash "$STATE/uninstall.sh" --docker --app "$APP" && touch "$STATE/uninstalled"
fi
if [ ! -f "$STATE/uninstalled" ] && [ "${DISPATCH_MORE_AUTO_UPDATE:-1}" != 0 ] && [ -f "$STATE/quick-install.sh" ]; then
  made_for="$(sed -n 's/.*"for_dispatcharr": *"\([^"]*\)".*/\1/p' "$STATE/manifest.json" 2>/dev/null | head -1)"
  image_has="$(sed -n "s/^__version__ = '\(.*\)'.*/\1/p" "$APP/version.py" 2>/dev/null)"
  if [ -n "$image_has" ] && [ "$made_for" != "$image_has" ]; then
    echo "Dispatch More: the release here is for Dispatcharr ${made_for:-?}, this image is $image_has; looking for the one made for it."
    STATE="$STATE" DISPATCHARR_APP="$APP" DISPATCH_MORE_LAYOUT=docker DISPATCH_MORE_FETCH_TIMEOUT="${DISPATCH_MORE_FETCH_TIMEOUT:-60}" \
      bash "$STATE/quick-install.sh" --fetch-only || true
  fi
fi
if [ -f "$STATE/uninstalled" ]; then
  echo "Dispatch More: uninstalled, starting stock Dispatcharr."
elif ! STATE="$STATE" DISPATCH_MORE_VIA=entrypoint bash "$STATE/install.sh" --docker --app "$APP"; then
  echo "Dispatch More: not applied (see above), starting stock Dispatcharr."
elif [ -f "$STATE/memory-recorder.sh" ]; then
  # The memory recorder, outside Dispatcharr's own processes (Diagnostics -> Memory -> History)
  bash "$STATE/memory-recorder.sh" docker "$STATE" "$APP" || true
fi
exec "$ORIGINAL" "$@"
