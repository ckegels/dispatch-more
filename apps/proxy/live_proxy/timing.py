"""How long a channel takes to start, phase by phase.

A channel start passes through several components, in more than one uWSGI worker: the request
arrives in one worker, while the provider connection and the data belong to whichever worker
owns the channel. Each of them marks its moment in one short-lived Redis hash, and the first
piece of video sent to the player logs the whole picture as a single line:

    Channel start CNN: slot 0.04s, provider connected 0.31s, first byte 0.52s,
    first keyframe 3.81s, first byte to player 0.55s (total 4.4s, slowest: first keyframe)

Measuring only: nothing here changes what is streamed, which stream is picked or which slot is
used. Every call is wrapped so a failure can never reach a stream request.

Why the keyframe matters: a player (and Plex's transcoder in particular) cannot show anything
until a keyframe arrives, so a channel that starts in the middle of a group of pictures looks
slow to start even though data is flowing from the first moment.
"""

import logging
import time

from .constants import TS_SYNC_BYTE

logger = logging.getLogger("live_proxy")

# One hash per channel start, thrown away shortly after: this is a measurement, not a log.
TIMING_KEY = "live:timing:{channel_uuid}"
TIMING_TTL = 120

# Recent channel starts for the Diagnostics page (same shape as the overlap's switches)
STARTS_KEY = "live:timing:starts"
START_KEY = "live:timing:start:{start_id}"
STARTS_KEPT = 200

# The phases, in the order they happen, with the name the page and the log line use
PHASES = (
    ("slot", "slot"),
    ("provider_connected", "provider connected"),
    ("first_byte", "first byte"),
    ("first_keyframe", "first keyframe"),
    ("first_byte_out", "first byte to player"),
)

TS_PACKET_SIZE = 188


def _key(channel_uuid) -> str:
    return TIMING_KEY.format(channel_uuid=channel_uuid)


def start(redis_client, channel_uuid, channel_name=None, client=None):
    """Called when a player asks for a channel: the moment everything else is measured from."""
    if not redis_client:
        return
    try:
        key = _key(channel_uuid)
        # Only the first request starts the clock; viewers joining a running channel do not
        if redis_client.hsetnx(key, "requested", str(time.time())):
            if channel_name:
                redis_client.hset(key, "channel", channel_name)
            if client:
                redis_client.hset(key, "client", client)
            redis_client.expire(key, TIMING_TTL)
    except Exception as e:
        logger.debug(f"Could not start timing for channel {channel_uuid}: {e}")


def mark(redis_client, channel_uuid, phase):
    """Remember when a phase was reached. The first time wins, so retries do not overwrite it."""
    if not redis_client or not channel_uuid:
        return
    try:
        key = _key(channel_uuid)
        if redis_client.exists(key):
            redis_client.hsetnx(key, phase, str(time.time()))
    except Exception as e:
        logger.debug(f"Could not mark {phase} for channel {channel_uuid}: {e}")


def find_keyframe(data) -> bool:
    """
    Whether this piece of transport stream contains a keyframe (a point a player can start at).

    Reads the random access indicator in the adaptation field of each 188-byte packet, which is
    what marks the start of a group of pictures. Anything that does not look like a transport
    stream is simply reported as "no keyframe" instead of raising.
    """
    try:
        view = memoryview(data)
        for offset in range(0, len(view) - TS_PACKET_SIZE + 1, TS_PACKET_SIZE):
            if view[offset] != TS_SYNC_BYTE:
                # Not aligned to packets (or not a transport stream at all)
                return False
            adaptation_field_control = (view[offset + 3] >> 4) & 0x3
            if adaptation_field_control not in (2, 3):
                continue
            field_length = view[offset + 4]
            if field_length == 0:
                continue
            if view[offset + 5] & 0x40:  # random access indicator
                return True
    except Exception:
        return False
    return False


def mark_keyframe(redis_client, channel_uuid, data) -> bool:
    """
    Mark the first keyframe in the data flowing from the provider. Returns True once it is
    found, so the caller can stop looking and the scan costs nothing for the rest of the stream.
    """
    if not redis_client or not channel_uuid:
        return True
    try:
        if not redis_client.exists(_key(channel_uuid)):
            # Nothing is being measured any more (the start is over)
            return True
        if not find_keyframe(data):
            return False
        mark(redis_client, channel_uuid, "first_keyframe")
        return True
    except Exception as e:
        logger.debug(f"Could not look for a keyframe on channel {channel_uuid}: {e}")
        return True


def _phases_from(marks):
    """(label, seconds) per phase that was reached, measured from the request."""
    requested = float(marks.get("requested", 0) or 0)
    if not requested:
        return []
    reached = []
    for field, label in PHASES:
        value = marks.get(field)
        if value:
            reached.append((label, float(value) - requested))
    return reached


def finish(redis_client, channel_uuid, channel_name=None):
    """
    Called when the first piece of video reaches the player: log the start as one line and
    remember it for the Diagnostics page. Runs once per channel start.
    """
    if not redis_client or not channel_uuid:
        return
    try:
        key = _key(channel_uuid)
        marks = {_as_str(k): _as_str(v) for k, v in (redis_client.hgetall(key) or {}).items()}
        if not marks.get("requested") or marks.get("logged"):
            return
        if not redis_client.hsetnx(key, "logged", "1"):
            # Another worker got there first
            return

        phases = _phases_from(marks)
        if not phases:
            return
        total = max(seconds for _label, seconds in phases)
        slowest = _slowest_phase(phases)
        name = channel_name or marks.get("channel") or channel_uuid
        logger.info(
            f"Channel start {name}: "
            + ", ".join(f"{label} {seconds:.2f}s" for label, seconds in phases)
            + f" (total {total:.2f}s, slowest: {slowest})"
        )
        start_id = _record_start(redis_client, marks, phases, total, slowest, name)
        # What the media server does with the video afterwards, added when it is known
        from . import media_servers

        media_servers.watch_start(
            redis_client, start_id, marks.get("client"), float(marks["requested"])
        )
        redis_client.expire(key, 10)
    except Exception as e:
        logger.debug(f"Could not log the start of channel {channel_uuid}: {e}")


def _slowest_phase(phases) -> str:
    """The phase that took longest on its own, which is where the waiting happened."""
    slowest, longest, previous = phases[0][0], phases[0][1], 0.0
    for label, seconds in phases:
        if seconds - previous > longest:
            slowest, longest = label, seconds - previous
        previous = seconds
    return slowest


def _record_start(redis_client, marks, phases, total, slowest, channel_name):
    """Keep this start for the Diagnostics page, like the overlap keeps its switches."""
    import secrets

    from . import probation

    now = time.time()
    start_id = secrets.token_hex(6)
    record = {
        "time": str(now),
        "channel": channel_name,
        "client": marks.get("client", ""),
        "total": f"{total:.2f}",
        "slowest": slowest,
        "phases": "|".join(f"{label}={seconds:.2f}" for label, seconds in phases),
    }
    ttl = probation.event_ttl(redis_client)
    redis_client.hset(START_KEY.format(start_id=start_id), mapping=record)
    redis_client.expire(START_KEY.format(start_id=start_id), ttl)
    redis_client.zadd(STARTS_KEY, {start_id: now})
    redis_client.zremrangebyrank(STARTS_KEY, 0, -(STARTS_KEPT + 1))
    redis_client.zremrangebyscore(STARTS_KEY, "-inf", now - ttl)
    redis_client.expire(STARTS_KEY, ttl)
    return start_id


def update_start(redis_client, start_id, **fields):
    """Add to a start that was already recorded, once a media server tells us more."""
    if not redis_client or not start_id:
        return
    try:
        key = START_KEY.format(start_id=start_id)
        if redis_client.exists(key):
            redis_client.hset(
                key,
                mapping={k: str(v) for k, v in fields.items() if v not in (None, "")},
            )
    except Exception as e:
        logger.debug(f"Could not add to channel start {start_id}: {e}")


def recent_starts(redis_client):
    """The last channel starts, newest first, for the Diagnostics page."""
    starts = []
    try:
        for start_id in redis_client.zrevrange(STARTS_KEY, 0, STARTS_KEPT - 1) or ():
            record = redis_client.hgetall(START_KEY.format(start_id=_as_str(start_id))) or {}
            if record:
                starts.append({_as_str(k): _as_str(v) for k, v in record.items()})
    except Exception as e:
        logger.debug(f"Could not read the recent channel starts: {e}")
    return starts


def _as_str(value):
    return value.decode() if isinstance(value, bytes) else value
