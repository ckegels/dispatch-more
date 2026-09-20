"""What a channel looks like while it runs, and what it looked like just before it stopped.

A channel that stops leaves almost nothing behind. The metadata is cleaned up, and what is
left says the state was "error" and nobody was watching, which was already obvious from the
channel having stopped. What is worth knowing is the shape of the minutes before it: whether
ffmpeg was falling behind, whether the source bitrate was collapsing, whether the viewers
left before the stream did or after. "The provider cut us off while everything was fine" and
"we were starving and died" need opposite fixes and look identical once it is over.

So a sample of every running channel is taken every few seconds and kept in a short ring,
and when a channel disappears its ring is frozen and kept for as long as the Diagnostics
page is set to keep things. Nothing here decides anything about a stream: it reads the
metadata the proxy already writes and writes to keys of its own.
"""

import json
import logging
import time

logger = logging.getLogger("live_proxy")

SETTINGS_KEY = "channel-health"
SETTINGS_CACHE_KEY = "live:health:settings"
SETTINGS_CACHE_TTL = 30

DEFAULTS = {
    # On: it costs a few writes a second and is the only record of why a channel stopped
    "enabled": True,
    # How often each running channel is sampled
    "every_seconds": 5,
}

# Held while a reading is being taken, so only one worker takes it (see sweep)
SWEEP_LOCK_KEY = "live:health:sweeping"

# How far back a rate is worked out over. The byte counter is written about as often as a
# reading is taken, so a shorter span catches updates unevenly and reads as nothing.
RATE_WINDOW = 20

# The samples of a channel that is running now, newest last
LIVE_KEY = "live:health:samples:{channel_id}"
# How many are kept: at five seconds apart, about three minutes of history
SAMPLES_KEPT = 36
# Dropped if nothing has been sampled for a while, so a crashed worker leaves nothing behind
LIVE_TTL = 900

# The channels that have stopped, newest first, each with the samples it ended on
STOPPED_KEY = "live:health:stopped"
STOPPED_KEPT = 50


def settings():
    """The settings, cached briefly: this is read on every sweep."""
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
        logger.debug(f"Could not read the channel health settings: {e}")
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
        defaults={"name": "Channel Health", "value": values},
    )
    try:
        cache.delete(SETTINGS_CACHE_KEY)
    except Exception:
        pass


def _number(metadata, field, default=0.0):
    """A number out of the metadata hash, which holds everything as text."""
    raw = metadata.get(field)
    if raw is None:
        return default
    try:
        return float(raw.decode() if isinstance(raw, bytes) else raw)
    except (TypeError, ValueError):
        return default


def _text(metadata, field, default=""):
    raw = metadata.get(field)
    if raw is None:
        return default
    return raw.decode() if isinstance(raw, bytes) else str(raw)


def sample(redis_client, channel_id) -> dict:
    """
    One reading of a channel: what it is doing and how well.

    Kept small, because one of these is written per channel per sweep and several hundred
    are held at a time. Everything in it is already in the metadata the proxy writes.
    """
    from .redis_keys import RedisKeys

    metadata = redis_client.hgetall(RedisKeys.channel_metadata(channel_id)) or {}
    if not metadata:
        return {}

    now = time.time()
    started = _number(metadata, "init_time")
    reading = {
        "at": now,
        # Carried in the reading rather than looked up when the channel stops: by then its
        # metadata is gone and all that is left to call it by is its id, which says nothing
        "name": _text(metadata, "channel_name"),
        "state": _text(metadata, "state", "unknown"),
        "uptime": round(now - started, 1) if started else 0,
        # How far behind real time ffmpeg is: under 1.0 for any length of time is a stream
        # that cannot keep up, and is what a stall looks like before it becomes one
        "speed": _number(metadata, "ffmpeg_speed"),
        "source_kbps": _number(metadata, "source_bitrate"),
        "output_kbps": _number(metadata, "ffmpeg_output_bitrate"),
        "clients": 0,
        # Data arriving is the difference between a slow stream and a stopped one
        "bytes": _number(metadata, "total_bytes"),
    }

    try:
        reading["clients"] = int(redis_client.scard(RedisKeys.clients(channel_id)) or 0)
    except Exception:
        pass
    return reading


def _channel_ids(redis_client):
    """The channels that are running, from the metadata each one writes while it does."""
    found = []
    # Scanned rather than asked for outright: this runs every few seconds, and asking Redis
    # for every key matching a pattern makes it walk the whole keyspace with nothing else
    # served meanwhile. On an installation with a lot in Redis that is felt everywhere.
    for key in redis_client.scan_iter(match="live:channel:*:metadata", count=500):
        key = key.decode() if isinstance(key, bytes) else key
        parts = key.split(":")
        if len(parts) >= 4:
            found.append(parts[2])
    return found


def sweep(redis_client):
    """
    Sample every running channel, and put away the ones that have stopped since last time.

    Called from the proxy's cleanup thread, which already wakes every few seconds. Never
    raises: a reading that cannot be taken is worth less than the channel it is about.
    """
    try:
        values = settings()
        if not values.get("enabled"):
            return 0

        # One reading per interval, not one per worker. Every worker process runs the
        # cleanup thread this is called from, so without this the channels are read four
        # times over and the handover of a channel that stopped races with itself.
        every = int(values.get("every_seconds") or DEFAULTS["every_seconds"])
        if not redis_client.set(SWEEP_LOCK_KEY, "1", nx=True, ex=max(1, every)):
            return 0

        running = set(_channel_ids(redis_client))
        for channel_id in running:
            reading = sample(redis_client, channel_id)
            if not reading:
                continue
            key = LIVE_KEY.format(channel_id=channel_id)
            redis_client.rpush(key, json.dumps(reading))
            redis_client.ltrim(key, -SAMPLES_KEPT, -1)
            redis_client.expire(key, LIVE_TTL)

        _put_away_the_stopped(redis_client, running)
        return len(running)
    except Exception as e:
        logger.debug(f"Could not sample the channels: {e}")
        return 0


def _put_away_the_stopped(redis_client, running):
    """
    A channel with samples but no metadata has stopped: keep what it ended on.

    This is the whole point of sampling. Once the metadata is gone there is no way back to
    what the channel was doing, so its last few minutes are moved somewhere they survive.
    """
    for key in redis_client.scan_iter(
        match=LIVE_KEY.format(channel_id="*"), count=500
    ):
        key = key.decode() if isinstance(key, bytes) else key
        channel_id = key.split(":")[-1]
        if channel_id in running:
            continue

        samples = _read_samples(redis_client, key)
        redis_client.delete(key)
        if not samples:
            continue

        # The name as it was while the channel ran. Asking now would only get its id back.
        named = next(
            (s["name"] for s in reversed(samples) if s.get("name")),
            channel_name(channel_id),
        )
        record = {
            "channel": named,
            "stopped_at": samples[-1].get("at", time.time()),
            "samples": with_rates(samples),
        }
        redis_client.lpush(STOPPED_KEY, json.dumps(record))
        redis_client.ltrim(STOPPED_KEY, 0, STOPPED_KEPT - 1)
        redis_client.expire(STOPPED_KEY, EVENT_TTL)
        logger.debug(f"Kept the last {len(samples)} readings of channel {channel_id}")


# What happened to each channel, for the health tab's "What happened": a small list,
# newest first. It was Stream Recovery that wrote to it, and this outlived that feature
# (see the handover, §5.3) -- anything worth telling somebody about a channel goes here.
EVENTS_KEY = "live:recovery:events"
EVENTS_KEPT = 200
EVENT_TTL = 24 * 3600


def channel_name(channel_id) -> str:
    """The channel's name, or its id: what happened to it matters more than its name."""
    try:
        from .utils import resolve_channel_display_name

        return resolve_channel_display_name(channel_id) or str(channel_id)
    except Exception:
        return str(channel_id)


def record_event(redis_client, channel_id, action, detail=""):
    """Remember what happened to a channel, for the health tab. Never raises."""
    if not redis_client:
        return
    try:
        event = {
            "time": time.time(),
            "channel": channel_name(channel_id),
            "action": action,
            "detail": detail,
        }
        redis_client.lpush(EVENTS_KEY, json.dumps(event))
        redis_client.ltrim(EVENTS_KEY, 0, EVENTS_KEPT - 1)
        redis_client.expire(EVENTS_KEY, EVENT_TTL)
    except Exception as e:
        logger.debug(f"Could not record a channel health event: {e}")


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
        logger.debug(f"Could not read the channel health events: {e}")
    return events


def _read_samples(redis_client, key):
    samples = []
    for raw in redis_client.lrange(key, 0, -1) or ():
        raw = raw.decode() if isinstance(raw, bytes) else raw
        try:
            samples.append(json.loads(raw))
        except ValueError:
            continue
    return with_rates(samples)


def with_rates(samples):
    """
    Work out what each reading carried, from how many bytes arrived over a window of them.

    ffmpeg's own speed and bitrate are only written while a stream profile is running one,
    so on a channel proxied straight through they are never there and every column about how
    well it is going would be empty. The bytes are always counted.

    Over a window rather than against the reading before it, because the byte counter is
    written to Redis about as often as these readings are taken. Compared one to the next,
    the two beat against each other: an interval that catches no update reads as nothing
    arriving, which looks exactly like a channel that has stopped carrying anything. Over a
    longer span every window contains several updates and the answer is steady.
    """
    for index, reading in enumerate(samples):
        rate = 0.0
        # The most recent reading far enough back to hold a few updates of the counter
        earlier = None
        for candidate in reversed(samples[:index]):
            if reading.get("at", 0) - candidate.get("at", 0) >= RATE_WINDOW:
                earlier = candidate
                break
        if earlier is None and index:
            earlier = samples[0]
        if earlier is not None:
            seconds = reading.get("at", 0) - earlier.get("at", 0)
            arrived = reading.get("bytes", 0) - earlier.get("bytes", 0)
            if seconds > 0 and arrived >= 0:
                rate = arrived * 8 / seconds / 1000
        reading["kbps"] = round(rate, 1)
    return samples


# What the page shows about a running channel beyond its readings: what it is playing, from
# where, what the picture is, and who is watching. Read when the page asks, never recorded:
# none of it is needed to tell afterwards how a channel ended.
DETAIL_FIELDS = (
    "stream_name", "stream_type", "resolution", "video_codec", "source_fps", "actual_fps",
    "ffmpeg_fps", "pixel_format", "audio_codec", "audio_channels", "sample_rate",
    "video_bitrate", "audio_bitrate", "source_bitrate", "stream_switch_reason",
    "stream_switch_time", "error_message", "error_time", "buffer_chunks",
)


def _profile_names(profile_ids):
    """{m3u profile id: (account name, profile name)}, in one query for every channel."""
    from apps.m3u.models import M3UAccountProfile

    ids = {int(i) for i in profile_ids if str(i).isdigit()}
    if not ids:
        return {}
    return {
        profile_id: (account, name)
        for profile_id, account, name in M3UAccountProfile.objects.filter(id__in=ids).values_list(
            "id", "m3u_account__name", "name"
        )
    }


def _stream_profile_name(value):
    """A stream profile by name ("Proxy", "FFmpeg"...), whether kept as its id or its name."""
    if not str(value or "").isdigit():
        return str(value or "")
    try:
        from core.models import StreamProfile

        return StreamProfile.objects.filter(id=int(value)).values_list("name", flat=True).first() or str(value)
    except Exception:
        return str(value)


def details(redis_client, channel_id, profile_names=None):
    """What a running channel is playing and who is watching it, for the page."""
    from .redis_keys import RedisKeys

    metadata = redis_client.hgetall(RedisKeys.channel_metadata(channel_id)) or {}
    if not metadata:
        return {}
    found = {field: _text(metadata, field) for field in DETAIL_FIELDS}
    profile_id = _text(metadata, "m3u_profile")
    account, profile = (profile_names or {}).get(int(profile_id), ("", "")) if profile_id.isdigit() else ("", "")
    found.update(
        account=account,
        profile=profile,
        stream_profile=_stream_profile_name(_text(metadata, "stream_profile")),
    )

    viewers = []
    now = time.time()
    try:
        from . import probation

        for client_id in redis_client.smembers(RedisKeys.clients(channel_id)) or ():
            client_id = client_id.decode() if isinstance(client_id, bytes) else str(client_id)
            client = redis_client.hgetall(RedisKeys.client_metadata(channel_id, client_id)) or {}
            if not client:
                continue
            agent = _text(client, "user_agent")
            ip = _text(client, "ip_address")
            connected = _number(client, "connected_at")
            viewers.append({
                "ip": ip,
                # The app, without its version; a media server or recording by its own name
                "app": probation.app_name(agent, ip) or agent[:60],
                "watching_for": round(now - connected, 1) if connected else 0,
                # What Dispatcharr is sending it, in kilobits
                "kbps": round(_number(client, "current_rate_KBps") * 8, 1),
                "format": _text(client, "output_format", "mpegts"),
            })
    except Exception as e:
        logger.debug(f"Could not read who is watching {channel_id}: {e}")
    found["viewers"] = sorted(viewers, key=lambda viewer: -viewer["watching_for"])
    return found


def running_now(redis_client):
    """Every channel that is running, with its readings, newest last."""
    channels = []
    try:
        from .redis_keys import RedisKeys

        ids = _channel_ids(redis_client)
        profile_names = _profile_names(
            _text(redis_client.hgetall(RedisKeys.channel_metadata(channel_id)) or {}, "m3u_profile")
            for channel_id in ids
        )
        for channel_id in ids:
            samples = _read_samples(
                redis_client, LIVE_KEY.format(channel_id=channel_id)
            )
            latest = sample(redis_client, channel_id)
            if not latest:
                continue
            # What it is carrying now, which on a channel with no ffmpeg behind it is the
            # only measure there is
            latest["kbps"] = samples[-1]["kbps"] if samples else 0.0
            channels.append({
                "channel": channel_name(channel_id),
                "now": latest,
                "samples": samples,
                "details": details(redis_client, channel_id, profile_names),
            })
    except Exception as e:
        logger.debug(f"Could not read what is running: {e}")
    return sorted(channels, key=lambda channel: channel["channel"])


def stopped_lately(redis_client, keep_seconds=None):
    """The channels that have stopped, newest first, each with what it ended on."""
    records = []
    try:
        cutoff = time.time() - keep_seconds if keep_seconds else 0
        for raw in redis_client.lrange(STOPPED_KEY, 0, STOPPED_KEPT - 1) or ():
            raw = raw.decode() if isinstance(raw, bytes) else raw
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if record.get("stopped_at", 0) >= cutoff:
                records.append(record)
    except Exception as e:
        logger.debug(f"Could not read the channels that stopped: {e}")
    return records
