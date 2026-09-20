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
print()
# Every client on a channel being played is in live:channel:<uuid>:clients. Not
# stream_profile:*, which is keyed by STREAM id and was the wrong thing to ask -- it is
# why this said "nothing is playing" while Plex was watching.
seen = 0
for key in redis_client.scan_iter("live:channel:*:clients"):
    name = key.decode() if isinstance(key, bytes) else key
    channel_uuid = name.split(":")[2]
    try:
        clients = list(probation._channel_clients(redis_client, channel_uuid))
    except Exception as e:
        print(f"  channel {channel_uuid}: could not read its viewers ({e})")
        continue
    for client in clients:
        seen += 1
        agent = client.get("user_agent") or ""
        ip = client.get("ip_address")
        yes = probation.is_media_server(agent, ip)
        print(f"  channel {channel_uuid}")
        print(f"      {'RECOGNISED as a media server' if yes else 'NOT recognised as a media server'}")
        print(f"      user agent: {agent!r}")
        print(f"      address:    {ip}")
        if not yes:
            print("      >> With scope 'media_servers' this channel is skipped, so a")
            print("         rotation on it can never be forgiven. This is the bug to")
            print("         report back: the user agent above matches none of")
            print("         jellyfin / emby / plex / lavf, and the address is not one")
            print("         of a server configured under Media Servers.")
        print()
if not seen:
    print("  NOTHING IS STREAMING THROUGH DISPATCHARR AT THIS MOMENT.")
    print()
    print("  This is not the same as Plex being open, or logged in, or showing its guide:")
    print("  what is looked for is a channel being pulled through the proxy right now.")
    print("  Start playing a live TV channel in Plex, leave it playing, run this again.")

print()
print("=" * 70)
print("4. HOW MANY RETRIES BEFORE DISPATCHARR GIVES UP (a different setting)")
print("=" * 70)
try:
    from apps.proxy.live_proxy.config_helper import ConfigHelper

    retries = ConfigHelper.max_retries()
    window = ConfigHelper.retry_window_seconds()
    print(f"  Maximum retry attempts     {retries}")
    print(f"  Retry window               {window}s ({window / 60:.0f} min)")
    print(f"  Stable before reconnect    {ConfigHelper.stable_connection_threshold()}s")
    print()
    print(f"  A stream is abandoned after {retries} failures inside {window / 60:.0f} minutes.")
    if retries > 20:
        print("  >> That is very high: a dead stream is retried for a long time instead of")
        print("     failing over to the next one, which looks like a channel that hangs.")
except Exception as e:
    print(f"  Could not read it: {e}")

print()
print("=" * 70)
print("5. WHAT THE LOG SAYS (beyond the day Redis keeps)")
print("=" * 70)
print("  Every forgiveness also writes a line. Redis keeps 24 hours; the journal keeps")
print("  longer, so this is the wider answer:")
print()
print("      journalctl -u dispatcharr --since '7 days ago' \\")
print("          | grep 'not counting it against'")
print()
print("  That phrase, and not 'Stream Recovery', which the settings being saved writes")
print("  too. Every line it prints is one stream a viewer did not lose; none at all")
print("  means it has never fired since the log begins.")
print()
