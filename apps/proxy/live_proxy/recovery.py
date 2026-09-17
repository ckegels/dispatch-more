"""Keeping a channel alive when the provider closes a connection that was working.

Providers close and rotate connections as normal behaviour, every few minutes on some of them.
Dispatcharr counts each close as a connection failure, and after MAX_RECONNECT_ATTEMPTS (3)
inside the retry window (30 minutes) it gives up on the stream, moves to the next one in the
channel, and eventually the channel dies. So the longer somebody watches, the likelier the
stream is to stop -- which is what "it works and then dies after a while" is.

What this adds: a close is forgiven when the connection had been delivering data for long
enough to call it working. Forgiven means the failure count is cleared rather than increased,
so a rotation is a fresh start instead of a step towards giving up. A stream that fails
quickly, over and over, still runs out of retries and still fails over to the next stream,
because a connection that never became stable is never forgiven.

It is a setting because it is a trade-off, not a free win: a source that plays for a minute
and drops, forever, would be kept instead of being replaced by the next stream in the channel.
That is why forgiveness can be limited to the channels a media server is watching (where a
long stream is the whole point and failover is most disruptive), and why there is a limit on
how often one channel may be forgiven per hour.
"""

import json
import logging
import time

logger = logging.getLogger("live_proxy")

SETTINGS_KEY = "stream-recovery"
SETTINGS_CACHE_KEY = "live:recovery:settings"
SETTINGS_CACHE_TTL = 30

DEFAULTS = {
    # Off by default: it changes how every channel behaves, so it is switched on deliberately
    "enabled": False,
    # How long a connection must have been delivering data to count as "it was working"
    "stable_seconds": 30,
    # "media_servers" (channels a media server is watching) or "all" (every channel)
    "scope": "media_servers",
    # A channel forgiven more often than this in an hour is not rotating, it is broken
    "max_per_hour": 10,
}

SCOPES = ("media_servers", "all")

# What happened to each channel, for the health tab: a small list per kind of event
EVENTS_KEY = "live:recovery:events"
EVENTS_KEPT = 200
EVENT_TTL = 24 * 3600
# How often a channel has been forgiven in the last hour
FORGIVEN_KEY = "live:recovery:forgiven:{channel_id}"


def settings():
    """The settings, cached briefly: this is read whenever a connection fails."""
    from django.core.cache import cache

    try:
        cached = cache.get(SETTINGS_CACHE_KEY)
        if cached is not None:
            return cached
    except Exception:
        pass

    values = dict(DEFAULTS)
    try:
        from core.models import CoreSettings

        stored = CoreSettings.objects.filter(key=SETTINGS_KEY).first()
        if stored and isinstance(stored.value, dict):
            values.update({k: v for k, v in stored.value.items() if k in DEFAULTS})
    except Exception as e:
        # Never let a settings read decide a stream's fate: without it, nothing changes
        logger.debug(f"Could not read the stream recovery settings: {e}")
        return dict(DEFAULTS)
    try:
        cache.set(SETTINGS_CACHE_KEY, values, SETTINGS_CACHE_TTL)
    except Exception:
        pass
    return values


def save_settings(values):
    from django.core.cache import cache

    from core.models import CoreSettings

    CoreSettings.objects.update_or_create(
        key=SETTINGS_KEY,
        defaults={"name": "Stream Recovery", "value": values},
    )
    try:
        cache.delete(SETTINGS_CACHE_KEY)
    except Exception:
        pass


def _watched_by_a_media_server(redis_client, channel_id) -> bool:
    """
    Whether a media server is watching this channel.

    Its own clients are asked, so this works whether or not a media server is configured under
    Media Servers: a request from Plex or Jellyfin is recognisable by itself.
    """
    from . import probation

    try:
        for client in probation._channel_clients(redis_client, channel_id):
            if probation.is_media_server(client.get("user_agent"), client.get("ip_address")):
                return True
    except Exception as e:
        logger.debug(f"Could not tell who is watching channel {channel_id}: {e}")
    return False


def forgive_disconnect(redis_client, channel_id, stable_for) -> bool:
    """
    Whether a connection that has just closed should be forgiven rather than counted.

    Says no, and changes nothing, unless it is switched on, the connection had been working
    for long enough, this channel is in scope, and it has not been forgiven too often already.
    """
    try:
        values = settings()
        if not values.get("enabled"):
            return False
        if stable_for < float(values.get("stable_seconds") or DEFAULTS["stable_seconds"]):
            return False
        if values.get("scope") != "all" and not _watched_by_a_media_server(
            redis_client, channel_id
        ):
            return False

        forgiven = _forgiven_this_hour(redis_client, channel_id)
        if forgiven >= int(values.get("max_per_hour") or DEFAULTS["max_per_hour"]):
            record_event(
                redis_client,
                channel_id,
                "gave up",
                f"forgiven {forgiven} times in an hour already, so this stream is not "
                f"rotating, it is failing",
            )
            return False

        record_event(
            redis_client,
            channel_id,
            "kept alive",
            f"the provider closed a connection that had been working for {stable_for:.0f}s",
        )
        return True
    except Exception as e:
        # Anything unexpected leaves Dispatcharr's own behaviour exactly as it was
        logger.debug(f"Could not decide whether to forgive a disconnect: {e}")
        return False


def _forgiven_this_hour(redis_client, channel_id) -> int:
    """How often this channel has been forgiven in the last hour, counted as it is asked."""
    key = FORGIVEN_KEY.format(channel_id=channel_id)
    count = redis_client.incr(key)
    if count == 1:
        redis_client.expire(key, 3600)
    return count - 1


def record_event(redis_client, channel_id, action, detail=""):
    """Remember what happened to a channel, for the health tab. Never raises."""
    if not redis_client:
        return
    try:
        event = {
            "time": time.time(),
            "channel": _channel_name(channel_id),
            "action": action,
            "detail": detail,
        }
        redis_client.lpush(EVENTS_KEY, json.dumps(event))
        redis_client.ltrim(EVENTS_KEY, 0, EVENTS_KEPT - 1)
        redis_client.expire(EVENTS_KEY, EVENT_TTL)
    except Exception as e:
        logger.debug(f"Could not record a stream health event: {e}")


def _channel_name(channel_id) -> str:
    """The channel's name, or its id: what happened to it matters more than what it is called."""
    try:
        from .utils import resolve_channel_display_name

        return resolve_channel_display_name(channel_id) or str(channel_id)
    except Exception:
        return str(channel_id)


def recent_events(redis_client):
    """What has happened to the channels lately, newest first."""
    events = []
    try:
        for raw in redis_client.lrange(EVENTS_KEY, 0, EVENTS_KEPT - 1) or ():
            raw = raw.decode() if isinstance(raw, bytes) else raw
            try:
                events.append(json.loads(raw))
            except ValueError:
                continue
    except Exception as e:
        logger.debug(f"Could not read the stream health events: {e}")
    return events
