"""How many other streams a channel could switch to for an arrTV device, right now.

arrTV gives up on a stream that does not start faster when the channel has several others it
could play, and waits a little longer when there is only one left: leaving a slow stream is
only worth it when there is somewhere to go. Only the server knows where: which of the
channel's streams this device can use, and which provider accounts have a connection free.
So each stream answer to arrTV carries the count (X-Dispatch-Alternatives), and faster failover
(app_devices.mark_fast_start) picks its own wait from it.

Off unless switched on ("alternatives" in app_devices' settings, with "Recognise each arrTV
device"); for any other player nothing is counted. The contract is fork/arrTV-integration.md.

A stream counts when the channel could be moved onto it for this device now: not the
"Could Not Dispatch" fallback (custom), an active account, within the device's quality limit
(what it decodes, away from home, what it stuttered down to), not playing on another channel,
and either on the account the channel already holds or on an account with a connection
free -- the same rules a stream of its own is picked by (app_own_streams._pick).
"""

import logging

from . import app_devices

logger = logging.getLogger("live_proxy")

HEADER = "X-Dispatch-Alternatives"


def enabled():
    """Whether the count is sent to arrTV (the X-Dispatch-Alternatives header)."""
    settings = app_devices.load_settings()
    return bool(settings.get("devices") and settings.get("alternatives"))


def wanted():
    """Whether the count is needed at all: sent to arrTV, or deciding faster failover's wait
    (a channel with nowhere to go is never hurried, whether or not arrTV is told)."""
    settings = app_devices.load_settings()
    return bool(settings.get("devices") and (settings.get("alternatives") or settings.get("fast_failover")))


def count(redis_client, viewer, channel):
    """
    How many of channel's streams it could switch to for this viewer, or None: switched off,
    not a declared arrTV device, not a channel (a stream of its own is never switched), or
    it could not be worked out. Never raises.
    """
    try:
        # arrTV first (a string check): every stream request passes here
        if not app_devices.is_declared(getattr(viewer, "server_device", None)):
            return None
        if redis_client is None or not hasattr(channel, "uuid") or not wanted():
            return None
        from apps.m3u.connection_pool import pool_has_capacity_for_profile

        from .app_own_streams import _fits
        from .constants import ChannelMetadataField
        from .redis_keys import RedisKeys

        limit = app_devices.quality_limit_for(viewer)
        current_raw = redis_client.hget(RedisKeys.channel_metadata(str(channel.uuid)), ChannelMetadataField.STREAM_ID)
        current_id = int(current_raw) if current_raw not in (None, b"", "") else None
        streams = list(channel.streams.select_related("m3u_account").order_by("channelstream__order"))
        current_account = next((s.m3u_account_id for s in streams if s.id == current_id), None)
        usable = 0
        for stream in streams:
            if stream.id == current_id or stream.is_custom or not stream.m3u_account:
                continue
            if stream.m3u_account.is_active is False:
                continue
            if limit and not _fits(stream, limit)[0]:
                continue
            if redis_client.get(f"stream_profile:{stream.id}"):
                # Playing on another channel: moving onto it would open a second connection
                continue
            if current_account is not None and stream.m3u_account_id == current_account:
                # The channel's own connection moves with it
                usable += 1
            elif any(
                profile.is_active and pool_has_capacity_for_profile(profile, redis_client, viewer)
                for profile in stream.m3u_account.profiles.all()
            ):
                usable += 1
        # Not running yet: one of the usable ones is the stream it starts on
        return usable if current_id is not None else max(0, usable - 1)
    except Exception as e:
        logger.debug(f"arrTV alternatives: could not count for {getattr(channel, 'name', channel)}: {e}")
        return None
