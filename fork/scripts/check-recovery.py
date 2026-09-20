# Whether Stream Recovery has ever done anything on this server, and if not, why not.
#
# Answers four questions in order, because they fail in that order:
#   1. Is it switched on, and with what settings?
#   2. Has it ever forgiven a disconnect (or declined to)? That is the whole question.
#   3. Is anything watching right now recognised as a media server? If Plex is not
#      recognised, the scope check excludes every channel and the feature can never fire.
#   4. How many retries does Dispatcharr allow before giving up on a stream? That is a
#      different setting, and on this setup it mattered more.
#
# Read-only. Opens no provider connection and disturbs nobody.
import json
import time

from apps.proxy.live_proxy import probation, recovery
from core.utils import RedisClient

redis_client = RedisClient.get_client()
print("=" * 70)
print("1. THE SETTINGS")
print("=" * 70)
values = recovery.settings()
for key in ("enabled", "stable_seconds", "scope", "max_per_hour"):
    print(f"  {key:16} {values.get(key)}")
if not values.get("enabled"):
    print("\n  >> Switched OFF. Nothing below can have happened.")

print()
print("=" * 70)
print("2. WHAT IT HAS ACTUALLY DONE (the last 24 hours)")
print("=" * 70)
raw = redis_client.lrange(recovery.EVENTS_KEY, 0, -1) or []
kept = declined = 0
for item in raw:
    try:
        event = json.loads(item)
    except (TypeError, ValueError):
        continue
    when = time.strftime("%d %b %H:%M:%S", time.localtime(event.get("time", 0)))
    print(f"  {when}  {event.get('action','?'):10} {event.get('channel','?')}")
    print(f"                        {event.get('detail','')}")
    if event.get("action") == "kept alive":
        kept += 1
    elif event.get("action") == "gave up":
        declined += 1
if not raw:
    print("  Nothing at all. It has never been asked to forgive a disconnect.")
    print("  Either the provider does not rotate connections, or no connection ever")
    print("  lasted stable_seconds, or nothing was recognised as being in scope (3).")
else:
    print(f"\n  {kept} stream(s) kept alive, {declined} declined as failing rather than rotating.")
    if kept:
        print("  >> Each 'kept alive' is a stream a viewer did NOT lose.")
    else:
        print("  >> It fired but never helped: those streams were failing, not rotating.")

print()
print("=" * 70)
print("3. IS ANYTHING RECOGNISED AS A MEDIA SERVER RIGHT NOW?")
print("=" * 70)
print("  (with scope 'media_servers', a channel no media server is watching is skipped)")
seen = 0
for key in redis_client.scan_iter("stream_profile:*"):
    name = key.decode() if isinstance(key, bytes) else key
    channel_id = name.rsplit(":", 1)[-1]
    try:
        clients = probation._channel_clients(redis_client, channel_id)
    except Exception as e:
        print(f"  channel {channel_id}: could not read its viewers ({e})")
        continue
    for client in clients:
        seen += 1
        agent = (client.get("user_agent") or "")[:60]
        ip = client.get("ip_address")
        yes = probation.is_media_server(agent, ip)
        print(f"  channel {channel_id}  {'MEDIA SERVER' if yes else 'not a media server'}")
        print(f"      user agent: {agent!r}")
        print(f"      address:    {ip}")
if not seen:
    print("  Nothing is playing, so there is nothing to recognise. Run this again while")
    print("  Plex is watching a channel: that is the check that matters.")

print()
print("=" * 70)
print("4. HOW MANY RETRIES BEFORE DISPATCHARR GIVES UP (a different setting)")
print("=" * 70)
try:
    from core.models import CoreSettings

    for key in ("max-reconnect-attempts", "reconnect-window", "max_retries"):
        row = CoreSettings.objects.filter(key=key).first()
        if row:
            print(f"  {key:26} {row.value}")
except Exception as e:
    print(f"  Could not read it: {e}")
print()
