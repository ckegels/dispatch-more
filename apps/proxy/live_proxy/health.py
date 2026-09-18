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

        from . import recovery

        record = {
            "channel": recovery._channel_name(channel_id),
            "stopped_at": samples[-1].get("at", time.time()),
            "samples": with_rates(samples),
        }
        redis_client.lpush(STOPPED_KEY, json.dumps(record))
        redis_client.ltrim(STOPPED_KEY, 0, STOPPED_KEPT - 1)
        redis_client.expire(STOPPED_KEY, recovery.EVENT_TTL)
        logger.debug(f"Kept the last {len(samples)} readings of channel {channel_id}")


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
    Work out what each reading actually carried, from how many bytes arrived since the last.

    ffmpeg's own speed and bitrate are only written while a stream profile is running one,
    so on a channel proxied straight through they are never there and every column about how
    well it is going would be empty. The bytes are always counted, and the rate between two
    readings is the honest measure of whether data is still arriving and how much.
    """
    previous = None
    for reading in samples:
        rate = 0.0
        if previous:
            seconds = reading.get("at", 0) - previous.get("at", 0)
            arrived = reading.get("bytes", 0) - previous.get("bytes", 0)
            if seconds > 0 and arrived >= 0:
                rate = arrived * 8 / seconds / 1000
        reading["kbps"] = round(rate, 1)
        previous = reading
    return samples


def running_now(redis_client):
    """Every channel that is running, with its readings, newest last."""
    channels = []
    try:
        from . import recovery

        for channel_id in _channel_ids(redis_client):
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
                "channel": recovery._channel_name(channel_id),
                "now": latest,
                "samples": samples,
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
