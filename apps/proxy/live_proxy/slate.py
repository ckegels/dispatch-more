"""A channel on the "Could Not Dispatch" slate keeps looking for a real stream, and then ends.

Every channel here ends in a custom stream, the could-not-dispatch plugin's slate: failover
lands on it when every real stream failed. Stock, it then plays for as long as anybody stays
connected -- on 2026-10-02 ┃BE┃ 24KITCHEN streamed the slate for more than twelve hours to an
arrTV nobody was watching, while its real streams had long come back.

The owner's cleanup tick asks `tick` about each channel it owns:

- **On the slate**: the stream playing is custom and the channel's last stream.
- **Every `slate_retry_seconds`** the next real stream (the channel's order, not custom, its
  account active, its profile with a free connection; in turn, so one bad stream is not asked
  every time) is switched to the way failover switches, in place, and must show bytes within
  VERIFY_SECONDS. It does not: back to the slate. One try at a time per channel, in a
  greenlet, so the cleanup loop never waits for it.
- **After `slate_limit_minutes`** on the slate the channel is stopped, which disconnects its
  viewers. For ENDED_SECONDS after that, a channel that lands on the slate again is stopped
  at its first look -- an unattended TV that reconnects does not start the twelve hours over.
  Its real streams are tried as usual when it starts, so a channel that works plays.

Off (Stream Check's `slate_retry`): nothing here runs, the slate plays as stock.
"""

import logging
import time

import gevent

logger = logging.getLogger("live_proxy")

# {since, last_try, tries, stream, profile}: when the channel landed on the slate and on which
SINCE_KEY = "live:slate:{channel_uuid}"
SINCE_TTL = 2 * 86400
# Set while a try runs, so a slow one is not started twice
TRYING_KEY = "live:slate:trying:{channel_uuid}"
TRYING_TTL = 60
# Set when the limit stopped the channel
ENDED_KEY = "live:slate:ended:{channel_uuid}"
ENDED_SECONDS = 30 * 60
VERIFY_SECONDS = 10


def _s(value):
    return value.decode() if isinstance(value, bytes) else value


def settings():
    """(on, retry seconds, limit seconds or 0)."""
    from apps.channels.stream_check import load_settings

    values = load_settings()
    return (bool(values.get("slate_retry")), int(values.get("slate_retry_seconds") or 60),
            int(values.get("slate_limit_minutes") or 0) * 60)


def _current(redis_client, channel_uuid):
    """(stream id, profile id) the channel plays, or (None, None)."""
    from .constants import ChannelMetadataField
    from .redis_keys import RedisKeys

    meta = RedisKeys.channel_metadata(channel_uuid)
    stream_id = _s(redis_client.hget(meta, ChannelMetadataField.STREAM_ID))
    if not stream_id:
        return None, None
    profile_id = _s(redis_client.get(f"stream_profile:{stream_id}"))
    return int(stream_id), int(profile_id) if profile_id and profile_id.isdigit() else None


def on_slate(channel, stream_id):
    """The stream is custom and the channel's last, after real ones: the fallback. A channel of
    custom streams only (somebody's own stream) is not on a slate, it is what it shows."""
    streams = list(channel.streams.order_by("channelstream__order").values_list("id", "is_custom"))
    return (bool(streams) and streams[-1] == (stream_id, True)
            and any(not custom for _id, custom in streams))


def candidates(channel, redis_client):
    """(stream, profile) for each real stream that could take the channel now, in its order."""
    from apps.m3u.connection_pool import pool_has_capacity_for_profile

    found = []
    for stream in channel.streams.select_related("m3u_account").order_by("channelstream__order"):
        account = stream.m3u_account
        if stream.is_custom or not account or not account.is_active:
            continue
        for profile in sorted(account.profiles.filter(is_active=True), key=lambda p: not p.is_default):
            if pool_has_capacity_for_profile(profile, redis_client):
                found.append((stream, profile))
                break
    return found


# The cleanup loop runs every few seconds; a channel is looked at this often (per worker)
LOOK_SECONDS = 10
_last_look = {}


def look(redis_client, channel_uuid):
    """`tick`, at most every LOOK_SECONDS per channel: it reads the settings and the channel."""
    now = time.time()
    if now - _last_look.get(channel_uuid, 0) < LOOK_SECONDS:
        return None
    _last_look[channel_uuid] = now
    for gone in [c for c, t in _last_look.items() if now - t > 3600]:
        del _last_look[gone]
    return tick(redis_client, channel_uuid, now)


def tick(redis_client, channel_uuid, now=None):
    """Looks at one channel this worker owns. Returns what it did, for the log and the tests."""
    from apps.channels.models import Channel

    enabled, retry_seconds, limit_seconds = settings()
    key = SINCE_KEY.format(channel_uuid=channel_uuid)
    if not enabled:
        return "off"
    now = now if now is not None else time.time()
    stream_id, profile_id = _current(redis_client, channel_uuid)
    channel = Channel.objects.filter(uuid=channel_uuid).first() if stream_id else None
    if channel is None or not on_slate(channel, stream_id):
        redis_client.delete(key)
        return "not on the slate"
    record = {_s(k): _s(v) for k, v in (redis_client.hgetall(key) or {}).items()}
    if not record:
        if redis_client.exists(ENDED_KEY.format(channel_uuid=channel_uuid)):
            logger.info(f"Slate: {channel.name} is back on the slate within {ENDED_SECONDS // 60} min "
                        f"of being ended there; stopped")
            _stop(channel_uuid)
            return "stopped again"
        redis_client.hset(key, mapping={"since": now, "last_try": now, "tries": 0,
                                        "stream": stream_id, "profile": profile_id or ""})
        redis_client.expire(key, SINCE_TTL)
        logger.info(f"Slate: {channel.name} is on the slate; a real stream is looked for every "
                    f"{retry_seconds} s")
        return "landed"
    since = float(record.get("since") or now)
    if limit_seconds and now - since >= limit_seconds:
        logger.info(f"Slate: {channel.name} was on the slate {int((now - since) // 60)} min; stopped")
        redis_client.set(ENDED_KEY.format(channel_uuid=channel_uuid), "1", ex=ENDED_SECONDS)
        redis_client.delete(key)
        _stop(channel_uuid)
        return "stopped"
    if now - float(record.get("last_try") or since) < retry_seconds:
        return "waiting"
    if not redis_client.set(TRYING_KEY.format(channel_uuid=channel_uuid), "1", nx=True, ex=TRYING_TTL):
        return "trying"
    tries = int(record.get("tries") or 0)
    redis_client.hset(key, mapping={"last_try": now, "tries": tries + 1})
    found = candidates(channel, redis_client)
    if not found:
        redis_client.delete(TRYING_KEY.format(channel_uuid=channel_uuid))
        return "nothing free"
    stream, profile = found[tries % len(found)]
    gevent.spawn(_try_safely, redis_client, channel, stream, profile, stream_id, profile_id)
    return "trying"


def _try_safely(redis_client, channel, stream, profile, slate_stream_id, slate_profile_id):
    try:
        try_stream(redis_client, channel, stream, profile, slate_stream_id, slate_profile_id)
    except Exception as e:
        logger.warning(f"Slate: trying {stream.name} for {channel.name} failed: {e}")
    finally:
        redis_client.delete(TRYING_KEY.format(channel_uuid=str(channel.uuid)))


def try_stream(redis_client, channel, stream, profile, slate_stream_id, slate_profile_id,
               sleep=gevent.sleep):
    """Switch to the stream; bytes within VERIFY_SECONDS keep it, else back to the slate."""
    from apps.channels.models import Stream
    from apps.m3u.models import M3UAccountProfile
    from apps.timeshift.priority import _switch, verify

    channel_uuid = str(channel.uuid)
    logger.info(f"Slate: trying {stream.name} (profile {profile.id}) for {channel.name}")
    if _switch(channel_uuid, stream, profile) and verify(redis_client, channel_uuid, stream.id,
                                                         seconds=VERIFY_SECONDS, sleep=sleep):
        logger.info(f"Slate: {channel.name} plays {stream.name} again; off the slate")
        redis_client.delete(SINCE_KEY.format(channel_uuid=channel_uuid))
        return True
    # Failover may have moved on already: to the slate, or to another stream that plays
    if _current(redis_client, channel_uuid)[0] != stream.id:
        logger.info(f"Slate: {stream.name} did not play for {channel.name}; failover moved it on")
        return False
    slate = Stream.objects.filter(id=slate_stream_id).first()
    slate_profile = M3UAccountProfile.objects.filter(id=slate_profile_id).first() if slate_profile_id else None
    if slate is not None and slate_profile is not None:
        _switch(channel_uuid, slate, slate_profile)
    logger.info(f"Slate: {stream.name} did not play for {channel.name}; back on the slate")
    return False


def _stop(channel_uuid):
    from .services.channel_service import ChannelService

    try:
        ChannelService.stop_channel(channel_uuid)
    except Exception as e:
        logger.warning(f"Slate: could not stop {channel_uuid}: {e}")
