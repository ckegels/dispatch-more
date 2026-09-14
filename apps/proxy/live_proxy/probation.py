"""
Probation slots ("Channel Switch Overlap") for live channel switches.

When every M3U profile a channel can use is at max_streams, a viewer who is already
watching is probably switching channels. On M3U accounts that allow it
(custom_properties "probation_enabled"), the new stream starts immediately on a
temporary slot one past the limit of the profile that viewer is watching on.
Viewers that are not watching get the normal limit error.

The same viewer means equal client IP, Dispatcharr user and device ID (a device ID is
written into M3U stream links by Dispatcharr). Anonymous viewers, with neither a user
nor a device ID, are matched on IP only and only on accounts that allow it
(custom_properties "probation_allow_anonymous").

The new stream is then "on probation":

- Some stream on the same profile ends within the account's probation window: the
  switch completed and the probation stream becomes a normal stream.
- The window expires with the profile still over its limit: the channel moves to
  another profile with free capacity, or, when none exists, the probation channel
  is stopped. Channels that were already playing are never stopped.

"Probation" is the internal name; the UI and logs call it the overlap slot and the
overlap window. See docs/channel-switch-overlap.md for the full design.
"""

import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Optional

import gevent

from .constants import ChannelMetadataField
from .redis_keys import RedisKeys

logger = logging.getLogger("live_proxy")

# Redis hash describing a channel's pending probation (see mark_probation).
PROBATION_KEY = "live:probation:{channel_uuid}"

# Extra slots a profile may use past max_streams while a stream is on probation.
# One per profile keeps the provider at most one connection over its limit.
PROBATION_EXTRA_CAPACITY = 1
# Per-account window bounds, mirrored by M3UAccountSerializer.probation_seconds.
DEFAULT_PROBATION_SECONDS = 10
MIN_PROBATION_SECONDS = 1
MAX_PROBATION_SECONDS = 120
# How often the monitor re-checks a probation; a completed switch is noticed this fast.
MONITOR_POLL_INTERVAL = 0.5

# Query parameter on stream links (written by the M3U output, read by stream_ts).
DEVICE_ID_PARAM = "device_id"
# Field added to the client metadata hash that ClientManager maintains.
DEVICE_ID_FIELD = "device_id"
# Stands in for the device ID in cached M3U content; replaced per response.
DEVICE_ID_PLACEHOLDER = "__dispatcharr_device_id__"
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# resolve_probation() outcomes
GONE = "gone"
ENDED = "ended"
CONFIRMED = "confirmed"
PENDING = "pending"
MIGRATED = "migrated"
STOPPED = "stopped"


@dataclass(frozen=True)
class Viewer:
    """Who is requesting a stream, as far as the request can tell."""

    ip: str
    user_id: Optional[int] = None
    device_id: Optional[str] = None

    @property
    def identified(self) -> bool:
        return self.user_id is not None or self.device_id is not None

    def matches(self, ip, user_id, device_id) -> bool:
        # A missing user or device ID only equals a missing one, so anonymous
        # viewers never match identified clients behind the same IP (and vice versa).
        return (
            self.ip == ip
            and self.user_id == user_id
            and self.device_id == device_id
        )


def probation_key(channel_uuid) -> str:
    return PROBATION_KEY.format(channel_uuid=channel_uuid)


def _account_props(m3u_account):
    from core.utils import custom_properties_as_dict

    return custom_properties_as_dict(getattr(m3u_account, "custom_properties", None))


def account_allows_probation(m3u_account) -> bool:
    # Strict "is True": custom_properties is free-form JSON and must opt in explicitly.
    return _account_props(m3u_account).get("probation_enabled") is True


def account_allows_anonymous(m3u_account) -> bool:
    return _account_props(m3u_account).get("probation_allow_anonymous") is True


def account_probation_seconds(m3u_account) -> int:
    try:
        seconds = int(
            _account_props(m3u_account).get("probation_seconds", DEFAULT_PROBATION_SECONDS)
        )
    except (TypeError, ValueError):
        return DEFAULT_PROBATION_SECONDS
    return min(max(seconds, MIN_PROBATION_SECONDS), MAX_PROBATION_SECONDS)


def any_account_allows_probation() -> bool:
    """Whether device IDs are worth adding to playlists at all."""
    from apps.m3u.models import M3UAccount

    return M3UAccount.objects.filter(
        is_active=True, custom_properties__probation_enabled=True
    ).exists()


def normalize_device_id(value):
    """Return a usable device ID, or None for missing or malformed values."""
    if isinstance(value, str) and _DEVICE_ID_RE.match(value):
        return value
    return None


def new_device_id() -> str:
    """Random per-playlist-download ID; 12 hex chars is plenty for one household."""
    return secrets.token_hex(6)


def viewer_from_request(request, user, client_ip):
    """Viewer identity for a live stream request, or None without a client IP."""
    if not client_ip:
        return None
    return Viewer(
        ip=client_ip,
        user_id=user.id if user is not None else None,
        device_id=normalize_device_id(request.GET.get(DEVICE_ID_PARAM)),
    )


def record_client_device(redis_client, channel_uuid, client_id, device_id):
    """
    Store a client's device ID next to the metadata ClientManager keeps for it.

    ClientManager already stores ip_address and user_id; the device ID is added to the
    same hash so find_profile_ids_watched_by() can compare all three.
    """
    if not redis_client or not device_id:
        return
    try:
        redis_client.hset(
            RedisKeys.client_metadata(str(channel_uuid), client_id),
            DEVICE_ID_FIELD,
            device_id,
        )
        logger.info(f"Probation: client {client_id} on channel {channel_uuid} has device ID {device_id}")
    except Exception as e:
        logger.debug(f"Could not record device ID for client {client_id}: {e}")


def _as_str(value):
    if isinstance(value, bytes):
        return value.decode()
    return value


def _client_user_id(value):
    # ClientManager stores "0" for connections without a Dispatcharr user.
    value = _as_str(value)
    if not value or value == "0":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def find_profile_ids_watched_by(redis_client, viewer):
    """
    Profile IDs of active channels that this viewer is watching.

    There is no index from profile to clients, so this scans channel metadata like
    Channel._pick_channel_to_preempt() does. It only runs when every profile of the
    requested channel is already full, which keeps it off the normal request path.
    """
    profile_ids = []
    if viewer is None:
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
                client = redis_client.hgetall(
                    RedisKeys.client_metadata(channel_uuid, _as_str(client_id))
                ) or {}
                client = {_as_str(k): _as_str(v) for k, v in client.items()}
                if viewer.matches(
                    client.get("ip_address"),
                    _client_user_id(client.get("user_id")),
                    normalize_device_id(client.get(DEVICE_ID_FIELD)),
                ):
                    profile_ids.append(int(profile_id))
                    break
    except Exception as e:
        logger.debug(f"Could not resolve profiles watched by {viewer}: {e}")

    return profile_ids


def mark_probation(redis_client, channel, stream_id, profile_id, seconds):
    """
    Record that a channel's newly reserved slot is over its profile limit.

    stream_id and profile_id let resolve_probation() notice when the channel stopped or
    failover moved it, which ends the probation without further action.
    """
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
    # Outlives the window so the monitor can still resolve it, but cannot linger
    # forever if the monitor never runs.
    redis_client.expire(key, int(seconds) + 120)


def has_pending_probation(redis_client, channel_uuid) -> bool:
    try:
        return bool(redis_client.exists(probation_key(channel_uuid)))
    except Exception:
        return False


def _clear(redis_client, channel_uuid):
    redis_client.delete(probation_key(channel_uuid))


def _profile_within_limits(profile, redis_client) -> bool:
    """Whether the profile (and its shared login pool, if any) is back at or under max_streams."""
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
    # Same order and default-profile-first rule as Channel.get_stream().
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
            # The regular stream switch path moves the profile counters
            # (Channel.update_stream_profile) and works from any worker.
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
        logger.info(
            f"Probation: channel {channel_uuid} stopped or changed stream before the window ended"
        )
        _clear(redis_client, channel_uuid)
        return ENDED

    profile = M3UAccountProfile.objects.select_related("m3u_account__server_group").filter(
        id=profile_id
    ).first()
    if profile is None:
        logger.info(f"Probation: profile {profile_id} for channel {channel_uuid} no longer exists")
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

    # Window expired while still over the limit: this was not a channel switch.
    # Clear first so a failing move or stop is not retried on every poll.
    _clear(redis_client, channel_uuid)

    channel = Channel.objects.filter(id=channel_id).first()
    if channel is not None and _migrate_to_free_profile(channel, stream_id, profile_id, redis_client):
        return MIGRATED

    from .services.channel_service import ChannelService

    # Only the probation channel is stopped; streams that were already playing stay.
    logger.warning(
        f"Probation: no stream ended on profile {profile_id} within the overlap window "
        f"and no other profile has capacity; stopping channel {channel_uuid}"
    )
    ChannelService.stop_channel(channel_uuid)
    return STOPPED


def _monitor(channel_uuid):
    """Poll resolve_probation() until the probation is no longer pending."""
    from django.db import close_old_connections

    # Greenlets hold a pooled DB connection until closed (see Plugins.md, DB connections).
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
    logger.info(f"Probation: watching channel {channel_uuid} until its overlap window ends")
    return gevent.spawn(_monitor, str(channel_uuid))
