"""When arrTV stutters, its channel moves to another stream at once.

Only the device knows it is stuttering. The server sees what arrives from the provider, and
on the user's own install not even how fast a device takes it (nginx buffers the stream in
between), so a stream that arrives in fits, or a connection that cannot carry an FHD
picture, looks fine from here while the picture on the TV stops every few seconds. arrTV
counts a stall the moment its player runs dry after the first frame, and says so
(POST /api/core/app-stall/); the channel then moves to its next stream straight away, in
place, the way a failover does -- the player keeps its connection.

What a dip is does not matter here. Stream Recovery (handover §5.3) and Stream Check were
about *judging a stream*, and a dip is no reason to call a stream broken. This is about a
viewer who is already watching a stuttering picture, where waiting costs more than trying
another stream. Nothing here ever counts against a stream: Stream Check is not told.

The rules, each for a reason:

- **Never a better stream, never the fallback.** A stutter is not fixed by more bits, and a
  stuttering picture is better than the "Could Not Dispatch" card. Same quality or lower, in
  the channel's own order after the stream it is on. Nothing to move to: nothing happens.
- **The first seconds of a stream are its own** (SETTLE_SECONDS after a start or a switch).
  A switch makes the player wait for the new stream, which arrTV sees as a stall; counting it
  would walk the channel through every stream it has.
- **Not back to a stream just left for stuttering** (LEFT_TTL), or two bad streams take turns.
- **A second stutter goes down in quality first**, and is remembered for that device (at home
  and away apart): the first switch may have been the provider's fault, which another stream
  of the same quality fixes; stuttering again on that one says the device's connection
  cannot carry it. The next channel it starts begins within that quality (app_devices).
- **Only a channel the device has to itself**, or one where every other viewer is an arrTV
  device stuttering too. A switch is felt by everyone on the channel, and somebody watching
  it fine should not lose their picture for somebody else's connection.

Off by default (the "stall_switch" setting, which needs devices recognised): off, nothing
reads the report and nothing changes.
"""

import logging
import time

from . import app_devices

logger = logging.getLogger("live_proxy")

# After a channel starts or changes stream, stalls are the new stream starting, not stutter
SETTLE_SECONDS = 10
# The streams a channel left because arrTV stuttered on them: {channel: set of stream ids}
LEFT_KEY = "live:app_stalls:left:{channel_uuid}"
LEFT_TTL = 10 * 60
# When each arrTV device on a channel last stuttered: {device key: time}
STALLED_KEY = "live:app_stalls:stalled:{channel_uuid}"
# Another device's stall this recent means it is stuttering too
STALLED_TOGETHER_SECONDS = 30
# One switch at a time per channel: two devices reporting the same stall switch once
SWITCHING_KEY = "live:app_stalls:switching:{channel_uuid}"
SWITCHING_TTL = SETTLE_SECONDS
# The best quality a device turned out to manage, at home or away: "FHD" / "HD" / "SD"
HELD_KEY = "live:app_stalls:held:{device}:{where}"
HELD_TTL = 24 * 3600


def enabled():
    settings = app_devices.load_settings()
    return bool(settings.get("devices") and settings.get("stall_switch"))


def _text(value):
    return value.decode() if isinstance(value, bytes) else value


def _channel_uuid(given):
    """The channel a report is about, from its UUID or its number id, or ""."""
    given = str(given or "").strip()
    if not given or not app_devices._CHANNEL_ID.match(given):
        return ""
    if not given.isdigit():
        return given
    try:
        from apps.channels.models import Channel

        uuid = Channel.objects.filter(id=int(given)).values_list("uuid", flat=True).first()
    except Exception:
        uuid = None
    return str(uuid) if uuid else ""


def _where(viewer):
    """"home", "away", or "any" when no home networks are set: learned apart."""
    at_home = app_devices._at_home(
        getattr(viewer, "ip", ""), app_devices.load_settings().get("home_networks")
    )
    return "any" if at_home is None else ("home" if at_home else "away")


def held_quality(redis_client, viewer):
    """The quality this device turned out to manage where it is now, or ""."""
    device = getattr(viewer, "server_device", None)
    if not redis_client or not app_devices.is_declared(device) or not enabled():
        return ""
    try:
        held = _text(redis_client.get(HELD_KEY.format(device=device, where=_where(viewer))))
    except Exception:
        return ""
    return held if held in app_devices.QUALITY_LIMITS else ""


def _hold(redis_client, viewer, label):
    if label not in app_devices.QUALITY_LIMITS:
        return
    key = HELD_KEY.format(device=viewer.server_device, where=_where(viewer))
    redis_client.setex(key, HELD_TTL, label)


def held_devices(redis_client):
    """Every device held to a lower quality now, for the settings page."""
    found = []
    try:
        for key in redis_client.scan_iter(match=HELD_KEY.format(device="*", where="*"), count=500):
            key = _text(key)
            rest = key[len("live:app_stalls:held:"):]
            device, _, where = rest.rpartition(":")
            label = _text(redis_client.get(key))
            ttl = redis_client.ttl(key)
            found.append({
                "device": device,
                "name": app_devices.device_name(redis_client, device) or device.split("|")[-1],
                "where": where,
                "quality": label,
                "until": time.time() + ttl if ttl and ttl > 0 else None,
            })
    except Exception as e:
        logger.debug(f"arrTV stutter: could not list the devices held to a quality: {e}")
    return sorted(found, key=lambda held: (held["name"], held["where"]))


def forget_held(redis_client, device=None, where=None):
    """Let a device (or every device) start at its best quality again."""
    for held in held_devices(redis_client):
        if device and held["device"] != device:
            continue
        if where and held["where"] != where:
            continue
        redis_client.delete(HELD_KEY.format(device=held["device"], where=held["where"]))


def _quality(stream):
    from apps.channels.channel_manager import quality_of

    label, rank, _probed = quality_of(stream.name, stream.stream_stats)
    return label, rank


def _next_stream(channel_uuid, current_id, left, lower_first):
    """
    The stream to move to, or None: the channel's next stream in its own order that is no
    better than the one it is on, not the fallback and not one just left. With lower_first,
    a lower quality is taken before one of the same.
    """
    from apps.channels.models import Stream

    from .url_utils import get_alternate_streams

    # Streams with a connection free for them, in the channel's order after the current one
    offered = [
        entry for entry in get_alternate_streams(channel_uuid, current_id)
        if str(entry["stream_id"]) not in left
    ]
    if not offered:
        return None
    streams = {s.id: s for s in Stream.objects.filter(id__in=[e["stream_id"] for e in offered])}
    current = Stream.objects.filter(id=current_id).first() if current_id else None
    current_label, current_rank = _quality(current) if current else ("", 2.5)

    fitting = []
    for position, entry in enumerate(offered):
        stream = streams.get(entry["stream_id"])
        if stream is None or stream.is_custom:
            continue
        label, rank = _quality(stream)
        # A stream (or a current one) that says nothing about its picture is not held
        # against it, as in app_devices.ordered_for; anything saying it is better is out
        if current_label and label and rank < current_rank:
            continue
        lower = bool(current_label and label and rank > current_rank)
        fitting.append((0 if (lower or not lower_first) else 1, position, stream, label))
    if not fitting:
        return None
    _order, _position, stream, label = min(fitting, key=lambda f: (f[0], f[1]))
    return stream, label, current_label


def report(redis_client, viewer, data):
    """
    arrTV stuttered on a channel. Moves the channel to its next stream when that is right,
    and says what was done: {"action": "switched", "stream": name} or
    {"action": "none", "reason": ...}.
    """
    data = data if isinstance(data, dict) else {}
    device = getattr(viewer, "server_device", None)
    if not app_devices.is_declared(device):
        return {"action": "none", "reason": "The device did not say which device it is."}
    channel_uuid = _channel_uuid(data.get("channel_uuid") or data.get("channel_id"))
    if not channel_uuid:
        return {"action": "none", "reason": "No such channel."}
    from . import app_own_streams

    # On a stream of its own (app_own_streams) others are on the channel: never switch it
    if app_own_streams.session_for(redis_client, viewer, channel_uuid):
        return {"action": "none", "reason": "This device plays this channel on a stream of its own."}

    from .constants import ChannelMetadataField
    from .probation import _channel_clients, _client_viewer
    from .redis_keys import RedisKeys

    metadata = {
        _text(k): _text(v)
        for k, v in (redis_client.hgetall(RedisKeys.channel_metadata(channel_uuid)) or {}).items()
    }
    if not metadata:
        return {"action": "none", "reason": "The channel is not playing."}
    clients = list(_channel_clients(redis_client, channel_uuid))
    mine = [c for c in clients if _client_viewer(c).server_device == device]
    if not mine:
        return {"action": "none", "reason": "This device is not watching that channel."}

    now = time.time()
    stalled_key = STALLED_KEY.format(channel_uuid=channel_uuid)
    redis_client.hset(stalled_key, device, str(now))
    redis_client.expire(stalled_key, LEFT_TTL)

    def _moment(field):
        try:
            return float(metadata.get(field) or 0)
        except ValueError:
            return 0.0

    began = max(_moment(ChannelMetadataField.INIT_TIME), _moment(ChannelMetadataField.STREAM_SWITCH_TIME))
    if began and now - began < SETTLE_SECONDS:
        return {"action": "none", "reason": "The stream has only just started."}

    # Everyone else on the channel has to be stuttering too, or they lose a good picture
    stalled = {_text(k): _text(v) for k, v in (redis_client.hgetall(stalled_key) or {}).items()}
    for client in clients:
        other = _client_viewer(client).server_device
        if other == device:
            continue
        try:
            recent = now - float(stalled.get(other) or 0) <= STALLED_TOGETHER_SECONDS
        except ValueError:
            recent = False
        if not app_devices.is_declared(other) or not recent:
            return {"action": "none", "reason": "Someone else is watching this channel without trouble."}

    switching_key = SWITCHING_KEY.format(channel_uuid=channel_uuid)
    if not redis_client.set(switching_key, "1", nx=True, ex=SWITCHING_TTL):
        return {"action": "none", "reason": "The channel is already changing stream."}
    try:
        answer = _switch(redis_client, viewer, channel_uuid, metadata, data)
    except Exception:
        redis_client.delete(switching_key)
        raise
    # Held only while a switch is under way: after one, the settle time keeps stalls out
    if answer["action"] != "switched":
        redis_client.delete(switching_key)
    return answer


def _switch(redis_client, viewer, channel_uuid, metadata, data):
    from .constants import ChannelMetadataField
    from .redis_keys import RedisKeys

    device = viewer.server_device

    left_key = LEFT_KEY.format(channel_uuid=channel_uuid)
    left = {_text(s) for s in (redis_client.smembers(left_key) or ())}
    current_id = metadata.get(ChannelMetadataField.STREAM_ID)
    try:
        current_id = int(current_id) if current_id else None
    except ValueError:
        current_id = None
    # Stuttering again after a switch here: the device's connection, not the provider
    again = bool(left)
    picked = _next_stream(channel_uuid, current_id, left, lower_first=again)
    name = app_devices.device_name(redis_client, device) or device.split("|")[-1]
    measured = ", ".join(
        f"{k} {data[k]}" for k in ("stalls", "feed_media_ratio", "worst_gap_ms", "bandwidth_kbps")
        if data.get(k) not in (None, "")
    )
    if picked is None:
        logger.info(f"arrTV stutter on channel {channel_uuid} ({name}): no other stream to move to")
        return {"action": "none", "reason": "The channel has no other stream to move to."}

    stream, label, current_label = picked
    from .services.channel_service import ChannelService

    result = ChannelService.change_stream_url(channel_uuid, target_stream_id=stream.id)
    if not result.get("success"):
        logger.warning(
            f"arrTV stutter on channel {channel_uuid} ({name}): could not move to {stream.name}: "
            f"{result.get('message') or result.get('error') or 'unknown'}"
        )
        return {"action": "none", "reason": "The channel could not change stream."}

    if current_id:
        redis_client.sadd(left_key, str(current_id))
        redis_client.expire(left_key, LEFT_TTL)
    redis_client.hset(
        RedisKeys.channel_metadata(channel_uuid), ChannelMetadataField.STREAM_SWITCH_REASON, "arrtv_stutter"
    )
    held = ""
    if again and label in app_devices.QUALITY_LIMITS and current_label and label != current_label:
        _hold(redis_client, viewer, label)
        held = f"; {name} starts within {label} from now on ({_where(viewer)})"
    detail = f"{name} stuttered{f' ({measured})' if measured else ''}: now on {stream.name}{held}"
    logger.info(f"arrTV stutter on channel {channel_uuid}: {detail}")
    from . import health

    health.record_event(redis_client, channel_uuid, "switched stream (arrTV stuttered)", detail)
    return {"action": "switched", "stream": stream.name}
