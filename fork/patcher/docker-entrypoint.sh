#!/bin/bash
# Docker: puts Dispatch More over the official image every time the container starts, then
# starts Dispatcharr as the image would. Point the container at it in docker-compose.yml:
#
#   entrypoint: ["/bin/bash", "/data/dispatch-more/docker-entrypoint.sh"]
#
# (a separate celery container: add  environment: DISPATCHARR_ENTRYPOINT=/app/docker/entrypoint.celery.sh)
#
# An image for another Dispatcharr version starts as stock: the release refuses it, and the log
# says so. After the page's Uninstall button, the next start puts stock back and stays stock;
# delete /data/dispatch-more/uninstalled to have it installed again.
STATE="$(cd "$(dirname "$0")" && pwd)"
APP="${DISPATCHARR_APP:-/app}"
ORIGINAL="${DISPATCHARR_ENTRYPOINT:-$APP/docker/entrypoint.sh}"

if [ -f "$STATE/requests/uninstall" ]; then
  STATE="$STATE" bash "$STATE/uninstall.sh" --docker --app "$APP" && touch "$STATE/uninstalled"
fi
if [ -f "$STATE/uninstalled" ]; then
  echo "Dispatch More: uninstalled, starting stock Dispatcharr."
elif ! STATE="$STATE" bash "$STATE/install.sh" --docker --app "$APP"; then
  echo "Dispatch More: not applied (see above), starting stock Dispatcharr."
fi
exec "$ORIGINAL" "$@"
