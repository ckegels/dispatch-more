"""
Probation slots for live channel switches.

When every M3U profile a channel can use is at max_streams, a viewer who is
already watching from the same client IP is probably switching channels. On M3U
accounts that allow it (custom_properties "probation_enabled"), the new stream
starts immediately on a temporary slot one past the limit of the profile that IP
is watching on. Requests from an IP that is not watching get the normal limit error.

The new stream is then "on probation":

- Some stream on the same profile ends within the account's probation window: the
  switch completed and the probation stream becomes a normal stream.
- The window expires with the profile still over its limit: the channel moves to
  another profile with free capacity, or, when none exists, the probation channel
  is stopped. Channels that were already playing are never stopped.

An IP match does not prove it is the same viewer (for example several players
behind one IP); resolution above keeps limits correct either way.
"""

import logging
import time

import gevent

from .constants import ChannelMetadataField
from .redis_keys import RedisKeys

logger = logging.getLogger("live_proxy")

PROBATION_KEY = "live:probation:{channel_uuid}"

# Extra slots a profile may use past max_streams while a stream is on probation.
PROBATION_EXTRA_CAPACITY = 1
DEFAULT_PROBATION_SECONDS = 10
MIN_PROBATION_SECONDS = 1
MAX_PROBATION_SECONDS = 120
MONITOR_POLL_INTERVAL = 0.5

GONE = "gone"
ENDED = "ended"
CONFIRMED = "confirmed"
PENDING = "pending"
MIGRATED = "migrated"
STOPPED = "stopped"


def probation_key(channel_uuid) -> str:
    return PROBATION_KEY.format(channel_uuid=channel_uuid)


def account_allows_probation(m3u_account) -> bool:
    from core.utils import custom_properties_as_dict

    props = custom_properties_as_dict(getattr(m3u_account, "custom_properties", None))
    return props.get("probation_enabled") is True


def account_probation_seconds(m3u_account) -> int:
    from core.utils import custom_properties_as_dict

    props = custom_properties_as_dict(getattr(m3u_account, "custom_properties", None))
    try:
        seconds = int(props.get("probation_seconds", DEFAULT_PROBATION_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_PROBATION_SECONDS
    return min(max(seconds, MIN_PROBATION_SECONDS), MAX_PROBATION_SECONDS)


def _as_str(value):
    if isinstance(value, bytes):
        return value.decode()
    return value


def find_profile_ids_watched_by_ip(redis_client, client_ip):
    """Profile IDs of active channels that a client from this IP is watching."""
    profile_ids = []
    if not client_ip:
        return profile_ids

    try:
        for metadata_key in redis_client.scan_iter(match="live:channel:*:metadata", count=500):
            metadata_key = _as_str(metadata_key)
            channel_uuid = metadata_key[len("live:channel:"):-len(":metadata")]
            profile_id = _as_str(
                redis_client.hget(metadata_key, ChannelMetadataField.M3U_PROFILE)
            )
            if not profile_id or int(profile_id) in profile_ids:
                continue
            for client_id in redis_client.smembers(RedisKeys.clients(channel_uuid)) or ():
                client_key = RedisKeys.client_metadata(channel_uuid, _as_str(client_id))
                if _as_str(redis_client.hget(client_key, "ip_address")) == client_ip:
                    profile_ids.append(int(profile_id))
                    break
    except Exception as e:
        logger.debug(f"Could not resolve profiles watched by {client_ip}: {e}")

    return profile_ids


def mark_probation(redis_client, channel, stream_id, profile_id, seconds):
    """Record that a channel's newly reserved slot is over its profile limit."""
    now = time.time()
    key = probation_key(channel.uuid)
    redis_client.hset(
        key,
        mapping={
            "channel_id": str(channel.id),
            "stream_id": str(stream_id),
            "profile_id": str(profile_id),
            "started_at": str(now),
            "deadline": str(now + seconds),
        },
    )
    redis_client.expire(key, int(seconds) + 120)


def has_pending_probation(redis_client, channel_uuid) -> bool:
    try:
        return bool(redis_client.exists(probation_key(channel_uuid)))
    except Exception:
        return False


def _clear(redis_client, channel_uuid):
    redis_client.delete(probation_key(channel_uuid))


def _profile_within_limits(profile, redis_client) -> bool:
    from apps.m3u.connection_pool import (
        get_credential_connection_count,
        get_profile_connection_count,
        get_enforced_server_group_for_profile,
    )

    if profile.max_streams == 0:
        return True
    if get_profile_connection_count(profile, redis_client) > profile.max_streams:
        return False
    if get_enforced_server_group_for_profile(profile):
        return get_credential_connection_count(profile, redis_client) <= profile.max_streams
    return True


def _migrate_to_free_profile(channel, current_stream_id, current_profile_id, redis_client) -> bool:
    """Move a probation channel to a stream on another profile that has capacity."""
    from apps.m3u.connection_pool import pool_has_capacity_for_profile
    from .services.channel_service import ChannelService
    from .url_utils import get_stream_info_for_switch

    channel_uuid = str(channel.uuid)
    streams = channel.streams.select_related("m3u_account").order_by("channelstream__order")
    for stream in streams:
        # Switching within the same stream row does not move profile counters.
        if stream.id == current_stream_id:
            continue
        account = stream.m3u_account
        if not account or not account.is_active:
            continue
        profiles = sorted(
            account.profiles.filter(is_active=True),
            key=lambda p: not p.is_default,
        )
        for profile in profiles:
            if profile.id == current_profile_id:
                continue
            if not pool_has_capacity_for_profile(profile, redis_client):
                continue
            info = get_stream_info_for_switch(channel_uuid, stream.id, profile.id)
            if "error" in info:
                continue
            result = ChannelService.change_stream_url(
                channel_uuid,
                info["url"],
                info["user_agent"],
                stream.id,
                profile.id,
                info.get("stream_name"),
            )
            if result.get("success"):
                logger.info(
                    f"Probation: moved channel {channel_uuid} from profile {current_profile_id} "
                    f"to stream {stream.id} profile {profile.id}"
                )
                return True
    return False


def resolve_probation(channel_uuid, redis_client=None, now=None) -> str:
    """
    Evaluate one probation record. Returns PENDING while the window is open and the
    profile is still over its limit; every other outcome clears the record.
    """
    from core.utils import RedisClient
    from apps.channels.models import Channel
    from apps.m3u.models import M3UAccountProfile

    redis_client = redis_client or RedisClient.get_client()
    now = now if now is not None else time.time()
    channel_uuid = str(channel_uuid)

    record = redis_client.hgetall(probation_key(channel_uuid)) or {}
    record = {_as_str(k): _as_str(v) for k, v in record.items()}
    if not record:
        return GONE

    try:
        channel_id = int(record["channel_id"])
        stream_id = int(record["stream_id"])
        profile_id = int(record["profile_id"])
        deadline = float(record["deadline"])
    except (KeyError, TypeError, ValueError):
        _clear(redis_client, channel_uuid)
        return GONE

    # The channel stopped, or failover already moved it off the profile.
    assigned_stream = _as_str(redis_client.get(f"channel_stream:{channel_id}"))
    assigned_profile = (
        _as_str(redis_client.get(f"stream_profile:{assigned_stream}"))
        if assigned_stream
        else None
    )
    if assigned_stream != str(stream_id) or assigned_profile != str(profile_id):
        _clear(redis_client, channel_uuid)
        return ENDED

    profile = M3UAccountProfile.objects.select_related("m3u_account__server_group").filter(
        id=profile_id
    ).first()
    if profile is None:
        _clear(redis_client, channel_uuid)
        return ENDED

    if _profile_within_limits(profile, redis_client):
        logger.info(
            f"Probation: channel {channel_uuid} confirmed on profile {profile_id} "
            f"after {now - float(record.get('started_at', now)):.1f}s"
        )
        _clear(redis_client, channel_uuid)
        return CONFIRMED

    if now < deadline:
        return PENDING

    _clear(redis_client, channel_uuid)

    channel = Channel.objects.filter(id=channel_id).first()
    if channel is not None and _migrate_to_free_profile(channel, stream_id, profile_id, redis_client):
        return MIGRATED

    from .services.channel_service import ChannelService

    logger.warning(
        f"Probation: no stream ended on profile {profile_id} within the probation window "
        f"and no other profile has capacity; stopping channel {channel_uuid}"
    )
    ChannelService.stop_channel(channel_uuid)
    return STOPPED


def _monitor(channel_uuid):
    from django.db import close_old_connections

    try:
        while True:
            try:
                outcome = resolve_probation(channel_uuid)
            except Exception as e:
                logger.error(f"Probation: error resolving channel {channel_uuid}: {e}", exc_info=True)
                return
            finally:
                close_old_connections()
            if outcome != PENDING:
                return
            gevent.sleep(MONITOR_POLL_INTERVAL)
    finally:
        close_old_connections()


def start_probation_monitor(channel_uuid):
    """Resolve a channel's probation in the background."""
    return gevent.spawn(_monitor, str(channel_uuid))
