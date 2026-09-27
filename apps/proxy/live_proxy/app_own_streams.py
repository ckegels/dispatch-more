"""An arrTV device that cannot use what a channel is playing gets a stream of its own.

A channel is one upstream for everybody on it: whoever starts it chooses the stream, and
everyone after joins that one. So when somebody at home is watching RTL ZWEI in 4K, a
Chromecast HD (which cannot decode 4K) or a phone away from home (whose connection cannot
carry it) joins the 4K stream and gets nothing, however many streams the channel has that it
could play. The user's rule: such a device is given another stream of the same channel,
from a provider with a connection free, and the others keep theirs.

Dispatcharr can already run a stream on its own, apart from any channel -- it is how a
stream is previewed -- keyed by the stream's hash rather than a channel's UUID. The request
is handed to that (stream_ts, before anything is reserved), so the device's own stream has
its own connection, is shared with any other device that needs the same stream, and ends
when the last of them leaves, all by stock's own machinery.

When, exactly:
- only for arrTV (a declared device, or its User-Agent), with the "own_stream" switch on;
- only when the channel is already playing (a device starting it chooses a stream it can
  play anyway, app_devices.ordered_for) on a stream better than the device can use: its own
  decoding limit (X-Dispatch-Max-Video), away from home, or a quality it stuttered down to;
- only onto a stream within that limit, never the fallback, and only one that costs nobody
  anything: one already running (joining it opens nothing), or one on an account with a
  connection free -- another provider's first, as the user put it. Nothing like that: the
  device joins the channel as before.

The channel the device asked for is remembered with the stream it was given, so the things
arrTV says about "its channel" -- the channel it is leaving, a stutter, a change of stream --
reach the device's own stream and never the channel everyone else is on.
"""

import logging

from . import app_devices

logger = logging.getLogger("live_proxy")

# {viewer: stream hash} per channel the viewer asked for, while it is on a stream of its own
OWN_KEY = "live:app_devices:own:{viewer}:{channel_uuid}"
OWN_TTL = 24 * 3600


def enabled():
    return bool(app_devices.load_settings().get("own_stream"))


def _text(value):
    return value.decode() if isinstance(value, bytes) else value


def _who(viewer):
    """The viewer as the remembered sessions are keyed: its device, or address and login."""
    device = getattr(viewer, "server_device", None)
    if app_devices.is_declared(device):
        return device
    return f"ip|{getattr(viewer, 'ip', '')}|{getattr(viewer, 'user_id', None) or 0}"


def _running(redis_client, session_id):
    return bool(redis_client.exists(f"live:channel:{session_id}:metadata"))


def session_for(redis_client, viewer, channel_uuid):
    """The stream hash this viewer is watching channel_uuid on, when it is on one of its own."""
    if not redis_client or not channel_uuid or viewer is None:
        return ""
    try:
        own = _text(redis_client.get(OWN_KEY.format(viewer=_who(viewer), channel_uuid=channel_uuid)))
    except Exception:
        return ""
    return own if own and _running(redis_client, own) else ""


def _fits(stream, limit):
    """Whether a stream is within a quality limit; one that says nothing is let through."""
    from apps.channels.channel_manager import QUALITY_LABELS, quality_of

    label, rank, _probed = quality_of(stream.name, stream.stream_stats)
    return not label or rank >= QUALITY_LABELS.index(limit), bool(label)


def _redirected(channel):
    try:
        return channel.get_stream_profile().is_redirect()
    except Exception:
        return False


def own_stream_for(redis_client, viewer, channel):
    """
    The stream this viewer should watch channel on instead of joining it, or None to join.
    Never raises: a viewer who cannot be given a stream of their own still gets the channel.
    """
    try:
        if (
            not redis_client
            or not hasattr(channel, "uuid")  # a stream asked for by its hash is already its own
            or not enabled()
            or not app_devices.is_arrtv(viewer)
        ):
            return None
        limit = app_devices.quality_limit_for(viewer)
        if not limit or _redirected(channel):
            # Redirected, every player goes to the provider itself: nothing is shared
            return None
        from apps.channels.models import Stream

        from .constants import ChannelMetadataField
        from .redis_keys import RedisKeys

        channel_uuid = str(channel.uuid)
        # Back on the stream it was given, while that still runs (a reconnect, a second tile)
        own = session_for(redis_client, viewer, channel_uuid)
        if own:
            found = Stream.objects.filter(stream_hash=own).first()
            if found:
                return found

        current_id = _text(redis_client.hget(RedisKeys.channel_metadata(channel_uuid), ChannelMetadataField.STREAM_ID))
        if not current_id:
            # Not playing: this device starts it, and starts it on a stream it can use
            return None
        current = Stream.objects.select_related("m3u_account").filter(id=int(current_id)).first()
        if current is None or _fits(current, limit)[0]:
            return None

        return _pick(redis_client, viewer, channel, current, limit)
    except Exception as e:
        logger.warning(f"arrTV own stream: could not look for one, joining the channel: {e}")
        return None


def _pick(redis_client, viewer, channel, current, limit):
    from apps.m3u.connection_pool import pool_has_capacity_for_profile

    choices = []
    for position, stream in enumerate(
        channel.streams.select_related("m3u_account").order_by("channelstream__order")
    ):
        if stream.id == current.id or stream.is_custom or not stream.m3u_account or not stream.stream_hash:
            continue
        if stream.m3u_account.is_active is False:
            continue
        fits, says = _fits(stream, limit)
        if not fits:
            continue
        # Already running somewhere: joining it opens nothing at the provider
        if redis_client.get(f"stream_profile:{stream.id}"):
            cost = 0
        elif any(
            profile.is_active and pool_has_capacity_for_profile(profile, redis_client, viewer)
            for profile in stream.m3u_account.profiles.all()
        ):
            # A free connection: another provider's before the one the channel is on
            cost = 1 if stream.m3u_account_id != current.m3u_account_id else 2
        else:
            continue
        # A stream that says what it is before one that says nothing, then the channel's order
        choices.append((cost, 0 if says else 1, position, stream))
    if not choices:
        logger.info(
            f"arrTV own stream: {channel.name} plays {current.name}, beyond {limit} for "
            f"{_who(viewer)}, and no stream within it has a connection free: joining it"
        )
        return None
    stream = min(choices, key=lambda c: c[:3])[3]
    redis_client.setex(
        OWN_KEY.format(viewer=_who(viewer), channel_uuid=str(channel.uuid)), OWN_TTL, stream.stream_hash
    )
    name = app_devices.device_name(redis_client, getattr(viewer, "server_device", None)) or _who(viewer)
    detail = f"plays {current.name} for others, beyond {limit} for {name}: {name} gets {stream.name}"
    logger.info(f"arrTV own stream: {channel.name} {detail}")
    from . import health

    health.record_event(redis_client, str(channel.uuid), "own stream for a device", detail)
    return stream
