#!/bin/bash
# Put Dispatcharr back to stock v0.31.0: every file any version of the modified build
# (probation-slots) has ever changed is reset to stock, and every file it added is removed.
# Then the page is rebuilt and the services restarted. Settings the modified build saved stay
# in the database, where stock Dispatcharr ignores them, so installing it again later picks
# up where it was.
#
#   bash uninstall-probation.sh
set -euo pipefail

APP=/opt/dispatcharr
TAG=v0.31.0
RAW="https://raw.githubusercontent.com/Dispatcharr/Dispatcharr/$TAG"

cd "$APP"
grep -q "__version__ = '0.31.0'" version.py || { echo "Dispatcharr here is not 0.31.0, stopping."; exit 1; }
FILES=""

# Every file any version of this patch has ever touched. A file an earlier version changed
# and this one does not is not in the patch, so resetting only the patch's files would
# leave the old change behind -- which is how apps/output/views.py kept calling a function
# that no longer exists, and /output/m3u answered 500. Each is reset to stock (or removed,
# if stock has no such file) whether or not this version still changes it.
RECORD=/root/.probation-slots-files
EVER="
apps/channels/api_urls.py
apps/channels/channel_manager.py
apps/channels/channel_manager_views.py
apps/channels/logo_library.py
apps/channels/logo_library_views.py
apps/channels/models.py
apps/channels/stream_check.py
apps/channels/stream_check_views.py
apps/channels/tasks.py
apps/channels/tests/test_channel_manager.py
apps/channels/tests/test_logo_library.py
apps/channels/tests/test_stream_check.py
apps/epg/tasks.py
apps/m3u/connection_pool.py
apps/m3u/serializers.py
apps/output/tests/test_m3u_device_id.py
apps/output/views.py
apps/proxy/live_proxy/diagnostics_views.py
apps/proxy/live_proxy/hdhr_tuner_views.py
apps/proxy/live_proxy/health.py
apps/proxy/live_proxy/input/manager.py
apps/proxy/live_proxy/media_servers.py
apps/proxy/live_proxy/media_server_tuner_views.py
apps/proxy/live_proxy/media_server_views.py
apps/proxy/live_proxy/output/ts/generator.py
apps/proxy/live_proxy/overlap_views.py
apps/proxy/live_proxy/probation.py
apps/proxy/live_proxy/recovery.py
apps/proxy/live_proxy/server.py
apps/proxy/live_proxy/tests/test_diagnostics_views.py
apps/proxy/live_proxy/tests/test_hdhr_tuners.py
apps/proxy/live_proxy/tests/test_health.py
apps/proxy/live_proxy/tests/test_media_servers.py
apps/proxy/live_proxy/tests/test_probation.py
apps/proxy/live_proxy/tests/test_reconnect_budget.py
apps/proxy/live_proxy/tests/test_recovery.py
apps/proxy/live_proxy/tests/test_timing.py
apps/proxy/live_proxy/timing.py
apps/proxy/live_proxy/url_utils.py
apps/proxy/live_proxy/views.py
apps/proxy/urls.py
core/api_views.py
core/tests/test_version_build.py
dispatcharr/settings.py
docs/channel-switch-overlap.md
frontend/src/api.js
frontend/src/App.jsx
frontend/src/components/AboutModal.jsx
frontend/src/components/diagnostics/ChannelHealth.jsx
frontend/src/components/diagnostics/ChannelStarts.jsx
frontend/src/components/diagnostics/ChannelSwitches.jsx
frontend/src/components/diagnostics/copyText.js
frontend/src/components/diagnostics/Diagnostics.jsx
frontend/src/components/diagnostics/__tests__/Diagnostics.test.jsx
frontend/src/components/forms/ChannelManagerLevers.jsx
frontend/src/components/forms/M3U.jsx
frontend/src/components/forms/StreamCheckSettings.jsx
frontend/src/components/forms/__tests__/ChannelManagerLevers.test.jsx
frontend/src/components/forms/__tests__/M3U.test.jsx
frontend/src/components/mediaservers/MediaServers.jsx
frontend/src/components/mediaservers/MediaServerTuners.jsx
frontend/src/components/mediaservers/StreamRecovery.jsx
frontend/src/components/mediaservers/__tests__/MediaServers.test.jsx
frontend/src/components/overlap/OverlapActivity.jsx
frontend/src/components/overlap/__tests__/OverlapActivity.test.jsx
frontend/src/components/Sidebar.jsx
frontend/src/components/tables/ChannelManagerTable.jsx
frontend/src/components/tables/logoLibraryColors.js
frontend/src/components/tables/LogoLibraryTable.jsx
frontend/src/components/tables/LogoPicker.jsx
frontend/src/components/tables/LogoSources.jsx
frontend/src/components/tables/StreamCheckTable.jsx
frontend/src/components/tables/StreamParts.jsx
frontend/src/components/tables/__tests__/ChannelManagerTable.test.jsx
frontend/src/components/tables/__tests__/LogoLibraryTable.test.jsx
frontend/src/components/tables/__tests__/LogoSources.test.jsx
frontend/src/components/tables/__tests__/StreamCheckTable.test.jsx
frontend/src/components/__tests__/AboutModal.test.jsx
frontend/src/config/navigation.js
frontend/src/config/settingsNav.js
frontend/src/pages/ChannelManager.jsx
frontend/src/pages/Logos.jsx
frontend/src/pages/Settings.jsx
frontend/src/pages/__tests__/ChannelManager.test.jsx
frontend/src/utils/versionLabel.js
version.py
"
RESET=$(printf '%s\n' $FILES $EVER $( [ -f "$RECORD" ] && cat "$RECORD" ) | sort -u)

BACKUP="/root/dispatcharr-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
EXISTING=$(for f in $RESET; do if [ -e "$f" ]; then echo "$f"; fi; done)
if [ -n "$EXISTING" ]; then
  tar czf "$BACKUP" $EXISTING || { echo "Could not write backup $BACKUP, stopping."; exit 1; }
  echo "Backup of current files: $BACKUP"
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Download every stock file first; change nothing until all downloads succeeded
for f in $RESET; do
  mkdir -p "$TMP/$(dirname "$f")"
  code=$(curl -sS -o "$TMP/$f" -w '%{http_code}' "$RAW/$f") || { echo "Download failed: $f"; exit 1; }
  case "$code" in
    200) ;;
    404) rm -f "$TMP/$f"; touch "$TMP/$f.new-file" ;;   # file is added by the patch
    *) echo "Unexpected HTTP $code for $f, stopping."; exit 1 ;;
  esac
done

for f in $RESET; do
  if [ -e "$TMP/$f.new-file" ]; then
    rm -f "$f"
  else
    mkdir -p "$(dirname "$f")"
    cp "$TMP/$f" "$f"
  fi
done
echo "Files reset to stock $TAG"

rmdir frontend/src/components/overlap/__tests__ frontend/src/components/overlap 2>/dev/null || true

# Nothing of the modified build is left, so there is nothing for a next install to reset
rm -f "$RECORD"

(cd frontend && npm run build)

SERVICES="dispatcharr dispatcharr-celery dispatcharr-celerybeat dispatcharr-daphne"
systemctl stop $SERVICES
# Nothing is streaming while the services are stopped, so connection slots left behind by
# channels that did not get to clean up are cleared (Redis keeps them across restarts).
for pattern in "profile_connections:*" "channel_stream:*" "stream_profile:*" \
               "profile_credential_release:*" "server_group_connections:*"; do
  redis-cli --scan --pattern "$pattern" | xargs -r redis-cli del >/dev/null
done
# What Stream Check kept while it ran
redis-cli --scan --pattern "stream-check:*" | xargs -r redis-cli del >/dev/null
echo "Cleared connection slots left from before the restart"
systemctl start $SERVICES
echo "Done: stock Dispatcharr $TAG. Check: grep -c __build__ $APP/version.py (should print 0)"
