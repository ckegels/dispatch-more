#!/bin/bash
# Proves the fork's pieces actually work against a running setup, rather than against
# fixtures. Every check prints what it asked for and what came back, so a failure says
# which call broke rather than only that something did.
#
#   bash verify-probation.sh
#
# Nothing here changes anything. The two checks that would (adding a tuner, changing a
# guide) are left out on purpose: those are done from the page, where they can be undone.

set -u

# ── What to talk to ──────────────────────────────────────────────────────────
DISPATCHARR="${DISPATCHARR:-http://192.168.2.142:9191}"
DISP_USER="${DISP_USER:-admin}"
DISP_PASS="${DISP_PASS:-}"

PLEX="${PLEX:-http://127.0.0.1:32400}"
PLEX_TOKEN="${PLEX_TOKEN:-${TOKEN:-}}"

JELLYFIN="${JELLYFIN:-}"
JELLYFIN_KEY="${JELLYFIN_KEY:-}"

PROFILE="${PROFILE:-austria}"      # a channel profile that exists
TUNERS="${TUNERS:-4}"

REDIS_CLI="${REDIS_CLI:-redis-cli}"

pass=0; fail=0; skip=0
ok()   { echo "  PASS  $1"; pass=$((pass+1)); }
no()   { echo "  FAIL  $1"; fail=$((fail+1)); }
meh()  { echo "  SKIP  $1"; skip=$((skip+1)); }
head_() { echo; echo "=== $1"; }

# Passes when the body matches, so a 200 that says the wrong thing still fails
check() { # check <name> <url> <grep-pattern> [curl args...]
  local name="$1" url="$2" want="$3"; shift 3
  local body code
  body=$(curl -s -w $'\n%{http_code}' "$@" "$url" 2>/dev/null)
  code=$(printf '%s' "$body" | tail -1)
  body=$(printf '%s' "$body" | sed '$d')
  if [ "$code" != "200" ]; then
    no "$name (HTTP $code)"
  elif printf '%s' "$body" | grep -q "$want"; then
    ok "$name"
  else
    no "$name (200, but no '$want' in the answer)"
    printf '%s' "$body" | head -c 200 | sed 's/^/        /'
    echo
  fi
}

# ── 1. The HDHomeRun whose tuner count comes from the address ────────────────
head_ "Dispatcharr: the tuner a media server is given"

BASE="$DISPATCHARR/proxy/hdhr/$PROFILE/tuners/$TUNERS"
check "discover.json answers"            "$BASE/discover.json"      '"TunerCount"'
check "the tuner count is the one asked" "$BASE/discover.json"      "\"TunerCount\": *$TUNERS"
check "its device id is its own"         "$BASE/discover.json"      "\-t$TUNERS"
check "lineup.json has channels"         "$BASE/lineup.json"        'GuideNumber'
check "lineup_status.json answers"       "$BASE/lineup_status.json" 'ScanInProgress'

echo "  -- Dispatcharr's own count, for comparison:"
curl -s "$DISPATCHARR/hdhr/$PROFILE/discover.json" \
  | grep -o '"TunerCount": *[0-9]*' | sed 's/^/     /'
echo "     (the number above is why the address carries one)"

# ── 2. The guide ─────────────────────────────────────────────────────────────
head_ "Dispatcharr: the guide that goes with it"

EPG="$DISPATCHARR/output/epg/$PROFILE?cachedlogos=false"
curl -s "$EPG" -o /tmp/verify-epg.xml
if [ -s /tmp/verify-epg.xml ]; then
  channels=$(grep -c '<channel id=' /tmp/verify-epg.xml)
  programmes=$(grep -c '<programme ' /tmp/verify-epg.xml)
  echo "  $channels channels, $programmes programmes, $(du -h /tmp/verify-epg.xml | cut -f1)"
  [ "$channels" -gt 0 ]   && ok "the guide has channels"   || no "the guide has no channels"
  [ "$programmes" -gt 0 ] && ok "the guide has programmes" || no "the guide has no programmes"

  # Every channel in the lineup should be in the guide, or it plays with nothing listed
  curl -s "$BASE/lineup.json" \
    | grep -o '"GuideNumber": *"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/' | sort -u > /tmp/verify-lineup
  grep -o '<channel id="[^"]*"' /tmp/verify-epg.xml \
    | sed 's/.*id="//;s/"//' | sort -u > /tmp/verify-guide
  both=$(comm -12 /tmp/verify-lineup /tmp/verify-guide | wc -l)
  echo "  lineup $(wc -l < /tmp/verify-lineup), guide $(wc -l < /tmp/verify-guide), matching $both"
  [ "$both" -gt 0 ] && ok "the lineup and the guide agree on channels" \
                    || no "nothing in the lineup is in the guide"

  # The logos the guide points at, which is the setting that is on by default
  if grep -q 'icon src="http' /tmp/verify-epg.xml; then
    first=$(grep -o '<icon src="[^"]*"' /tmp/verify-epg.xml | head -1 | sed 's/.*src="//;s/"//')
    echo "  first logo: $first"
    logo_code=$(curl -s -o /dev/null -w '%{http_code}' "$first")
    [ "$logo_code" = "200" ] && ok "that logo can be fetched from here ($logo_code)" \
                             || no "that logo answered $logo_code from here"
    echo "     (a player off this network has to reach it too, which is the whole point"
    echo "      of pointing the guide at the original addresses)"
  else
    no "the guide carries no logos at all"
  fi
else
  no "the guide returned nothing"
fi

# ── 3. The fork's own endpoints ──────────────────────────────────────────────
head_ "Dispatcharr: the pages this fork adds"

if [ -z "$DISP_PASS" ]; then
  meh "no DISP_PASS set, so the signed-in endpoints are not checked"
  echo "     run:  DISP_PASS=yourpassword bash $0"
else
  JWT=$(curl -s -X POST "$DISPATCHARR/api/accounts/token/" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$DISP_USER\",\"password\":\"$DISP_PASS\"}" \
    | sed -n 's/.*"access":"\([^"]*\)".*/\1/p')
  if [ -z "$JWT" ]; then
    no "could not sign in as $DISP_USER"
  else
    ok "signed in as $DISP_USER"
    AUTH=(-H "Authorization: Bearer $JWT")

    check "Diagnostics answers"        "$DISPATCHARR/proxy/diagnostics/"          '"starts"'    "${AUTH[@]}"
    check "  channel switches are in it" "$DISPATCHARR/proxy/diagnostics/"        '"events"'    "${AUTH[@]}"
    check "  channel health is in it"  "$DISPATCHARR/proxy/diagnostics/"          '"running"'   "${AUTH[@]}"
    check "  what stopped is in it"    "$DISPATCHARR/proxy/diagnostics/"          '"stopped"'   "${AUTH[@]}"
    check "Stream Recovery answers"    "$DISPATCHARR/proxy/stream-recovery/"      '"enabled"'   "${AUTH[@]}"
    check "Media Servers answers"      "$DISPATCHARR/proxy/media-servers/"        '"servers"'   "${AUTH[@]}"

    echo "  -- what Dispatcharr thinks is running now:"
    curl -s "${AUTH[@]}" "$DISPATCHARR/proxy/diagnostics/" > /tmp/verify-diag.json
    python3 - <<'EOF' 2>/dev/null || echo "     (could not read it)"
import json
d = json.load(open("/tmp/verify-diag.json"))
running = d.get("running") or []
for c in running:
    now = c["now"]
    name = c["channel"][:34]
    kbps = now.get("kbps") or 0
    print("     %-34s %-8s %s watching  %.0f kbps  %d readings"
          % (name, now["state"], now["clients"], kbps, len(c["samples"])))
if not running:
    print("     (nothing playing)")
health = d.get("channel_health") or {}
print("     recording: %s every %ss" % (health.get("enabled"), health.get("every_seconds")))
stopped = d.get("stopped") or []
print("     %d stopped channels kept" % len(stopped))
for c in stopped[:3]:
    last = (c["samples"] or [{}])[-1]
    print("       %-34s ran %.0fs, ended with %s watching, %.0f kbps"
          % (c["channel"][:34], last.get("uptime") or 0,
             last.get("clients"), last.get("kbps") or 0))
EOF

    echo "  -- the servers it knows:"
    curl -s "${AUTH[@]}" "$DISPATCHARR/proxy/media-servers/" > /tmp/verify-servers.json
    python3 - <<'EOF' 2>/dev/null || echo "     (could not read it)"
import json
for s in (json.load(open("/tmp/verify-servers.json")).get("servers") or []):
    print("     %-14s %-32s online=%s  %d sessions  %s"
          % (s.get("name"), s.get("url"), s.get("online"),
             len(s.get("sessions") or []), s.get("error") or ""))
EOF
  fi
fi

# ── 4. What is being recorded, in Redis ──────────────────────────────────────
head_ "Channel health: the readings themselves"

if command -v "$REDIS_CLI" >/dev/null 2>&1; then
  samples=$($REDIS_CLI --scan --pattern 'live:health:samples:*' 2>/dev/null | wc -l)
  stopped=$($REDIS_CLI llen live:health:stopped 2>/dev/null)
  echo "  $samples channels being read, $stopped stopped channels kept"
  [ "$samples" -gt 0 ] && ok "readings are being taken" \
    || meh "no readings (nothing playing, or recording is off)"
  echo "  -- who the media servers say is watching:"
  $REDIS_CLI get live:media_servers:sessions > /tmp/verify-sessions.json 2>/dev/null
  python3 - <<'EOF' 2>/dev/null || echo "     (nothing)"
import json
raw = open("/tmp/verify-sessions.json").read().strip()
if not raw or raw == "(nil)":
    print("     (no media server has reported anyone watching)")
else:
    for s in json.loads(raw):
        print("     %-12s %-16s live=%-5s %s"
              % (s.get("user") or "?", s.get("player") or "?",
                 s.get("live"), s.get("title")))
EOF
  echo "  -- which channel each device is on (this is what a switch is seen from):"
  for k in $($REDIS_CLI --scan --pattern 'live:media_servers:device_channel:*' 2>/dev/null); do
    echo "     ${k##*device_channel:} -> $($REDIS_CLI get "$k" 2>/dev/null)"
  done
else
  meh "no $REDIS_CLI here; run this on the machine Dispatcharr is on"
fi

# ── 5. Plex ──────────────────────────────────────────────────────────────────
head_ "Plex"

if [ -z "$PLEX_TOKEN" ]; then
  meh "no PLEX_TOKEN set"
else
  check "it answers"            "$PLEX/identity?X-Plex-Token=$PLEX_TOKEN"            'MediaContainer'
  check "its DVRs can be read"  "$PLEX/livetv/dvrs?X-Plex-Token=$PLEX_TOKEN"         'MediaContainer'
  check "its tuners can be read" "$PLEX/media/grabbers/devices?X-Plex-Token=$PLEX_TOKEN" 'MediaContainer'
  check "its sessions can be read" "$PLEX/status/sessions?X-Plex-Token=$PLEX_TOKEN"   'MediaContainer'

  echo "  -- its DVR, the tuners in it and the guide each one uses:"
  curl -s "$PLEX/livetv/dvrs?X-Plex-Token=$PLEX_TOKEN" -o /tmp/verify-dvr.xml
  python3 - <<'EOF' 2>/dev/null || echo "     (could not read it)"
import urllib.parse, xml.etree.ElementTree as ET
root = ET.parse('/tmp/verify-dvr.xml').getroot()
if not len(root):
    print("     (no DVR: a tuner in none is registered and never used)")
for dvr in root:
    lineups = [l.get('id','') for l in dvr.findall('Lineup')]
    print(f"     DVR {dvr.get('key')}  {len(dvr.findall('Device'))} channel sources, "
          f"{len(lineups)} guides")
    for dev in dvr.findall('Device'):
        m = dev.findall('ChannelMapping')
        on = sum(1 for x in m if x.get('enabled') == '1')
        print(f"       {str(dev.get('title')):16} {dev.get('uri')}")
        print(f"       {'':16} {len(m)} channels, {on} enabled, state {dev.get('state')}")
    for l in lineups:
        addr = urllib.parse.unquote(l.split('/',3)[-1].split('#')[0]) if '//' in l else l
        print(f"       guide: {addr}")
EOF
  echo "     (every channel source wants a guide of its own; a source with none plays"
  echo "      with nothing listed against it)"
fi

# ── 6. Jellyfin ──────────────────────────────────────────────────────────────
head_ "Jellyfin"

if [ -z "$JELLYFIN" ] || [ -z "$JELLYFIN_KEY" ]; then
  meh "no JELLYFIN / JELLYFIN_KEY set"
else
  JF=(-H "X-Emby-Token: $JELLYFIN_KEY")
  check "it answers"                "$JELLYFIN/System/Info"                  'Version'     "${JF[@]}"
  check "its live TV can be read"   "$JELLYFIN/System/Configuration/livetv"  'TunerHosts'  "${JF[@]}"
  check "its sessions can be read"  "$JELLYFIN/Sessions"                     '\['          "${JF[@]}"
  check "its guide refresh is there" "$JELLYFIN/ScheduledTasks"              'RefreshGuide' "${JF[@]}"

  echo "  -- its tuners and guides:"
  curl -s "${JF[@]}" "$JELLYFIN/System/Configuration/livetv" > /tmp/verify-jf.json
  python3 - <<'EOF' 2>/dev/null || echo "     (could not read it)"
import json
c = json.load(open("/tmp/verify-jf.json"))
for t in c.get("TunerHosts") or []:
    print("     tuner: %-16s %-46s %s tuners"
          % (t.get("FriendlyName"), t.get("Url"), t.get("TunerCount")))
for g in c.get("ListingProviders") or []:
    print("     guide: %-52s all tuners=%s" % (g.get("Path"), g.get("EnableAllTuners")))
if not (c.get("TunerHosts") or c.get("ListingProviders")):
    print("     (no tuners and no guides)")
EOF

  echo "  -- who it says is watching:"
  curl -s "${JF[@]}" "$JELLYFIN/Sessions" > /tmp/verify-jf-sessions.json
  python3 - <<'EOF' 2>/dev/null || echo "     (could not read it)"
import json
found = False
for s in json.load(open("/tmp/verify-jf-sessions.json")):
    item = s.get("NowPlayingItem") or {}
    if not item:
        continue
    found = True
    live = item.get("Type") == "TvChannel" or bool(item.get("ChannelId"))
    print("     %-12s %-22s live=%-5s %s: %s"
          % (s.get("UserName"), s.get("DeviceId"), live,
             item.get("Type"), item.get("Name")))
if not found:
    print("     (nobody is playing anything)")
EOF
  echo "     (live=True is what the overlap needs: a viewer it cannot see is a viewer"
  echo "      whose old channel is never stopped)"
fi

# ── What it came to ──────────────────────────────────────────────────────────
echo
echo "════════════════════════════════════════════"
echo "  $pass passed, $fail failed, $skip skipped"
echo "════════════════════════════════════════════"
[ "$fail" -eq 0 ] || exit 1
