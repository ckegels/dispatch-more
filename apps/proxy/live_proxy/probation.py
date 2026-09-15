"""
Channel Switch Overlap ("probation slots") for live channels.

Everything here is per M3U account (custom_properties) and does nothing unless the
account has "probation_enabled":

- Overlap: when every profile a channel can use is full, a viewer already watching on one
  of them may start the new channel at once on one extra slot. If a stream on that profile
  ends within the window it was a channel switch and nothing else happens; otherwise the
  channel moves to a profile with a free slot, or this new channel is stopped.
- Stop Skipped Channels ("probation_stop_skipped"): channels a viewer only watched for a
  moment are stopped when it requests the next one, so fast surfing frees slots.
- When Switching Channels ("probation_account_preference"): "same" keeps a viewer's next
  channel on the account it is on or just left, "alternate" starts it on another account
  with a free slot first, "order" (default) follows the channel's stream order.

A viewer is identified by client IP plus Dispatcharr user and/or a device ID that the M3U
output adds to stream links. Viewers with neither (HDHomeRun, media servers) are anonymous:
matched on IP only, and only where "probation_allow_anonymous" is set. Streams that were
already playing, or that someone else also watches, are never stopped.

"Probation" is the internal name; the UI and logs say overlap. See
docs/channel-switch-overlap.md for the full design.
"""

import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Optional

import gevent
from django.db.models.signals import post_delete, post_save

from .constants import ChannelMetadataField, ChannelState
from .redis_keys import RedisKeys

logger = logging.getLogger("live_proxy")

# Redis hash describing a channel's pending probation (see mark_probation).
PROBATION_KEY = "live:probation:{channel_uuid}"
# Profile a viewer was last assigned, for the account preference after its old channel is
# gone (for example stopped as skipped). Watched channels are found by scanning.
LAST_PROFILE_KEY = "live:probation:last_profile:{viewer}"
LAST_PROFILE_TTL = 60
# Set while a skipped (or idle) channel is being stopped in the background, so the next
# request during fast channel surfing does not stop it again, and a request for that channel
# waits for the stop to finish. Removed when the stop is done.
SKIPPED_STOPPING_KEY = "live:probation:stopping:{channel_uuid}"
SKIPPED_STOPPING_TTL = 30
# How long a request for a channel that is still being stopped waits before giving up
STOP_WAIT_SECONDS = 5
STOP_WAIT_POLL_INTERVAL = 0.1
# Slots released by a viewer's channel and held for that viewer during the overlap window
# (hash: viewer key -> "expiry|shared login counter key"), so a waiting request cannot take
# it mid-switch.
HELD_SLOTS_KEY = "live:probation:held:{profile_id}"
# The same holds on the shared login counter of a Server Group (hash: viewer key ->
# "expiry|profile id"), because other accounts in the group count against that login too.
HELD_LOGIN_SLOTS_KEY = "live:probation:held_login:{credential_key}"
# Viewers that joined a channel (set of viewer keys). Clients are removed when they leave,
# so this is how a channel waiting out the Channel Shutdown Delay is still recognised as
# the viewer's channel.
CHANNEL_VIEWERS_KEY = "live:probation:viewers:{channel_uuid}"
CHANNEL_VIEWERS_TTL = 24 * 60 * 60
# Set before releasing a channel whose slot must not be held: the overlap stopped it because
# it was not a switch, or its viewer already has a slot for its next channel.
NO_HOLD_KEY = "live:probation:no_hold:{channel_uuid}"

# Extra slots a profile may use past max_streams while a stream is on probation.
# One per profile keeps the provider at most one connection over its limit.
PROBATION_EXTRA_CAPACITY = 1
# Per-account window bounds, mirrored by M3UAccountSerializer.probation_seconds.
DEFAULT_PROBATION_SECONDS = 10
MIN_PROBATION_SECONDS = 1
MAX_PROBATION_SECONDS = 120
# How often the monitor re-checks a probation; a completed switch is noticed this fast.
MONITOR_POLL_INTERVAL = 0.5
# Channels with a probation record (set), so any worker can find probations whose monitor
# is gone, for example after a worker restart.
PENDING_PROBATIONS_KEY = "live:probation:pending"
# Renewed by the running monitor on every check. Longer than a stream move takes
# (ChannelService.change_stream_url waits up to 15 s), so a live monitor never loses it.
MONITOR_LEASE_KEY = "live:probation:monitor:{channel_uuid}"
MONITOR_LEASE_TTL = 20
# Moving or stopping an expired probation is retried this often when it keeps failing.
MAX_RESOLVE_ATTEMPTS = 3

# Query parameter on stream links (written by the M3U output, read by stream_ts).
DEVICE_ID_PARAM = "device_id"
# Field added to the client metadata hash that ClientManager maintains.
DEVICE_ID_FIELD = "device_id"
# Stands in for the device ID in cached M3U content; replaced per response.
DEVICE_ID_PLACEHOLDER = "__dispatcharr_device_id__"
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Media servers download one playlist for all of their viewers, so a device ID would make
# every viewer look like the same device. Matched against the default User-Agents
# (for example "Jellyfin-Server/10.10.7", "Emby/4.8.10.0", "PlexMediaServer/1.41.0").
_MEDIA_SERVER_RE = re.compile(r"jellyfin|emby|plex", re.IGNORECASE)
# DVR recordings request channels through the proxy with this User-Agent
# (core.utils.dispatcharr_dvr_user_agent). They are not viewers switching channels.
_RECORDING_USER_AGENT_PREFIX = "Dispatcharr-DVR"

# Whether any M3U account has the overlap enabled, cached for all workers and cleared when
# an account is saved or deleted. Everything here checks it first, so Dispatcharr does no
# extra work while the feature is not used.
IN_USE_CACHE_KEY = "live:probation:in_use"
IN_USE_CACHE_TTL = 60

# stream_ts retries get_stream() for a few seconds while every profile is full, so each
# "not used" reason is logged once per channel and viewer within this interval.
NOT_USED_LOG_INTERVAL = 10

# resolve_probation() outcomes
GONE = "gone"
ENDED = "ended"
CONFIRMED = "confirmed"
PENDING = "pending"
MIGRATED = "migrated"
STOPPED = "stopped"
FAILED = "failed"


# ── Viewer identity ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Viewer:
    """Who is requesting a stream, as far as the request can tell."""

    ip: str
    user_id: Optional[int] = None
    device_id: Optional[str] = None
    # A DVR recording: never uses the overlap, account preferences or held slots
    recording: bool = False

    @property
    def identified(self) -> bool:
        return self.user_id is not None or self.device_id is not None

    def matches(self, ip, user_id, device_id) -> bool:
        # A missing user or device ID only equals a missing one, so anonymous
        # viewers never match identified clients behind the same IP (and vice versa).
        return (
            not self.recording
            and self.ip == ip
            and self.user_id == user_id
            and self.device_id == device_id
        )


def is_viewer_request(viewer) -> bool:
    """Whether a request can take part in the overlap: a live viewer, not a recording."""
    return viewer is not None and not viewer.recording


def probation_key(channel_uuid) -> str:
    return PROBATION_KEY.format(channel_uuid=channel_uuid)


# ── Logging ──────────────────────────────────────────────────────────────────

# (channel uuid, viewer, reason) -> monotonic time last logged, per worker process
_not_used_logged = {}


def log_not_used(channel_uuid, viewer, reason):
    """Log why the overlap was not used, without repeating it on every retry attempt."""
    key = (str(channel_uuid), viewer, reason)
    now = time.monotonic()
    last = _not_used_logged.get(key)
    if last is not None and now - last < NOT_USED_LOG_INTERVAL:
        return
    # Forget expired entries so long-running workers do not accumulate keys
    for stale in [k for k, t in _not_used_logged.items() if now - t >= NOT_USED_LOG_INTERVAL]:
        del _not_used_logged[stale]
    _not_used_logged[key] = now
    logger.info(f"Probation: not used for channel {channel_uuid}: {reason}")


# ── Account settings ─────────────────────────────────────────────────────────


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


def account_stops_skipped_channels(m3u_account) -> bool:
    return _account_props(m3u_account).get("probation_stop_skipped") is True


ACCOUNT_PREFERENCE_ORDER = "order"
ACCOUNT_PREFERENCE_SAME = "same"
ACCOUNT_PREFERENCE_ALTERNATE = "alternate"
ACCOUNT_PREFERENCES = (ACCOUNT_PREFERENCE_ORDER, ACCOUNT_PREFERENCE_SAME, ACCOUNT_PREFERENCE_ALTERNATE)


def account_switch_preference(m3u_account) -> str:
    """Which account a viewer's next channel prefers ("order", "same" or "alternate")."""
    props = _account_props(m3u_account)
    preference = props.get("probation_account_preference")
    if preference in ACCOUNT_PREFERENCES:
        return preference
    # Earlier builds stored "Stay On Same Account" as a boolean
    if props.get("probation_sticky") is True:
        return ACCOUNT_PREFERENCE_SAME
    return ACCOUNT_PREFERENCE_ORDER


def account_keeps_viewers(m3u_account) -> bool:
    return account_switch_preference(m3u_account) == ACCOUNT_PREFERENCE_SAME


def account_alternates_viewers(m3u_account) -> bool:
    return account_switch_preference(m3u_account) == ACCOUNT_PREFERENCE_ALTERNATE


def any_account_stops_skipped_channels() -> bool:
    """Whether stop_skipped_channels() can apply to any account at all."""
    from apps.m3u.models import M3UAccount

    return M3UAccount.objects.filter(
        is_active=True,
        custom_properties__probation_enabled=True,
        custom_properties__probation_stop_skipped=True,
    ).exists()


def any_account_allows_probation() -> bool:
    from apps.m3u.models import M3UAccount

    return M3UAccount.objects.filter(
        is_active=True, custom_properties__probation_enabled=True
    ).exists()


def in_use() -> bool:
    """Whether any active M3U account has the overlap enabled (cached, see IN_USE_CACHE_KEY)."""
    from django.core.cache import cache

    try:
        cached = cache.get(IN_USE_CACHE_KEY)
    except Exception:
        cached = None
    if cached is None:
        try:
            cached = int(any_account_allows_probation())
        except Exception as e:
            # Never let this break a stream request; ask again next time
            logger.debug(f"Could not check whether Channel Switch Overlap is in use: {e}")
            return False
        try:
            cache.set(IN_USE_CACHE_KEY, cached, IN_USE_CACHE_TTL)
        except Exception:
            pass
    return bool(cached)


def forget_in_use(**_kwargs):
    """Signal receiver: an account changed, so read the setting again on next use."""
    from django.core.cache import cache

    try:
        cache.delete(IN_USE_CACHE_KEY)
    except Exception as e:
        logger.debug(f"Could not clear {IN_USE_CACHE_KEY}: {e}")


post_save.connect(forget_in_use, sender="m3u.M3UAccount", dispatch_uid="probation_in_use_save")
post_delete.connect(forget_in_use, sender="m3u.M3UAccount", dispatch_uid="probation_in_use_delete")


# ── Device IDs and requests ──────────────────────────────────────────────────


def normalize_device_id(value):
    """Return a usable device ID, or None for missing or malformed values."""
    if isinstance(value, str) and _DEVICE_ID_RE.match(value):
        return value
    return None


def is_media_server(user_agent) -> bool:
    return bool(user_agent and _MEDIA_SERVER_RE.search(user_agent))


def fill_device_id(m3u_content, device_id):
    """
    Put this response's device ID into playlist content built with DEVICE_ID_PLACEHOLDER,
    or remove the parameter when device_id is None (media servers).
    """
    if device_id:
        return m3u_content.replace(DEVICE_ID_PLACEHOLDER, device_id)
    # The device ID is always the last query parameter on a stream link
    return m3u_content.replace(f"&{DEVICE_ID_PARAM}={DEVICE_ID_PLACEHOLDER}", "").replace(
        f"?{DEVICE_ID_PARAM}={DEVICE_ID_PLACEHOLDER}", ""
    )


def new_device_id() -> str:
    """Random per-playlist-download ID; 12 hex chars is plenty for one household."""
    return secrets.token_hex(6)


def is_recording(user_agent) -> bool:
    return bool(user_agent and user_agent.startswith(_RECORDING_USER_AGENT_PREFIX))


def viewer_from_request(request, user, client_ip):
    """Viewer identity for a live stream request, or None without a client IP."""
    if not client_ip:
        return None
    user_agent = request.META.get("HTTP_USER_AGENT")
    if is_recording(user_agent):
        return Viewer(ip=client_ip, recording=True)
    device_id = normalize_device_id(request.GET.get(DEVICE_ID_PARAM))
    # A media server may still use links from a playlist it fetched before it was
    # recognised; its viewers must stay anonymous either way.
    if is_media_server(user_agent):
        device_id = None
    return Viewer(
        ip=client_ip,
        user_id=user.id if user is not None else None,
        device_id=device_id,
    )


def _channel_viewers_key(channel_uuid) -> str:
    return CHANNEL_VIEWERS_KEY.format(channel_uuid=channel_uuid)


def record_client_viewer(redis_client, channel_uuid, client_id, viewer):
    """
    Remember which viewer a newly registered client is, while the overlap is in use.

    ClientManager already stores ip_address and user_id; the device ID is added to the
    same hash so find_profile_ids_watched_by() can compare all three. The viewer is also
    added to the channel's viewer set, which outlives the client (see CHANNEL_VIEWERS_KEY).
    """
    if not redis_client or not is_viewer_request(viewer) or not in_use():
        return
    channel_uuid = str(channel_uuid)
    try:
        if viewer.device_id:
            redis_client.hset(
                RedisKeys.client_metadata(channel_uuid, client_id),
                DEVICE_ID_FIELD,
                viewer.device_id,
            )
            logger.debug(
                f"Probation: client {client_id} on channel {channel_uuid} has device ID "
                f"{viewer.device_id}"
            )
        viewers_key = _channel_viewers_key(channel_uuid)
        redis_client.sadd(viewers_key, _viewer_key(viewer))
        redis_client.expire(viewers_key, CHANNEL_VIEWERS_TTL)
    except Exception as e:
        logger.debug(f"Could not record viewer for client {client_id}: {e}")


# ── Active channels and their clients (Redis) ────────────────────────────────


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


def _active_channels(redis_client):
    """Yield (channel uuid, profile id or None) for every live channel in Redis."""
    # There is no index from profile to channels, so scan like
    # Channel._pick_channel_to_preempt() does.
    for metadata_key in redis_client.scan_iter(match="live:channel:*:metadata", count=500):
        metadata_key = _as_str(metadata_key)
        channel_uuid = metadata_key[len("live:channel:"):-len(":metadata")]
        profile_id = _as_str(redis_client.hget(metadata_key, ChannelMetadataField.M3U_PROFILE))
        yield channel_uuid, int(profile_id) if profile_id else None


def _channel_clients(redis_client, channel_uuid):
    """Yield the metadata hash of each client on a channel, as a dict of strings."""
    for client_id in redis_client.smembers(RedisKeys.clients(channel_uuid)) or ():
        client = redis_client.hgetall(
            RedisKeys.client_metadata(channel_uuid, _as_str(client_id))
        ) or {}
        yield {_as_str(k): _as_str(v) for k, v in client.items()}


def _client_viewer(client) -> Viewer:
    return Viewer(
        client.get("ip_address"),
        _client_user_id(client.get("user_id")),
        normalize_device_id(client.get(DEVICE_ID_FIELD)),
        recording=is_recording(client.get("user_agent")),
    )


def _is_viewer(viewer, client) -> bool:
    return _client_viewer(client) == viewer


def _being_stopped(redis_client, channel_uuid) -> bool:
    """Whether a stop is already under way (dashboard, delete, refresh, skipped channel)."""
    if redis_client.exists(RedisKeys.channel_stopping(channel_uuid)) or redis_client.exists(
        SKIPPED_STOPPING_KEY.format(channel_uuid=channel_uuid)
    ):
        return True
    state = _as_str(
        redis_client.hget(RedisKeys.channel_metadata(channel_uuid), ChannelMetadataField.STATE)
    )
    return state == ChannelState.STOPPING


def _waiting_for_shutdown(redis_client, channel_uuid) -> bool:
    """
    Whether a channel has no clients left and only runs on because of the Channel Shutdown
    Delay: its last client left (last_client_disconnect_time) and no stop has started yet.

    A channel that is still starting is never idle: its first client may not have registered
    yet, and a disconnect time can be left over from an earlier run of the same channel.
    """
    state = _as_str(
        redis_client.hget(RedisKeys.channel_metadata(channel_uuid), ChannelMetadataField.STATE)
    )
    return (
        state is not None
        and state not in ChannelState.PRE_ACTIVE
        and bool(redis_client.exists(RedisKeys.last_client_disconnect(channel_uuid)))
        and not redis_client.scard(RedisKeys.clients(channel_uuid))
        and not _being_stopped(redis_client, channel_uuid)
    )


def _only_viewer_was(redis_client, channel_uuid, viewer) -> bool:
    viewer_keys = {
        _as_str(key) for key in redis_client.smembers(_channel_viewers_key(channel_uuid)) or ()
    }
    return viewer_keys == {_viewer_key(viewer)}


def find_profile_ids_watched_by(redis_client, viewer):
    """
    Profile IDs of the live channels this viewer is watching, including a channel it was
    the only viewer of that is waiting out the Channel Shutdown Delay.
    """
    profile_ids = []
    if not is_viewer_request(viewer):
        return profile_ids

    try:
        for channel_uuid, profile_id in _active_channels(redis_client):
            if profile_id is None or profile_id in profile_ids:
                continue
            clients = list(_channel_clients(redis_client, channel_uuid))
            if any(_is_viewer(viewer, client) for client in clients) or (
                not clients
                and _waiting_for_shutdown(redis_client, channel_uuid)
                and _only_viewer_was(redis_client, channel_uuid, viewer)
            ):
                profile_ids.append(profile_id)
    except Exception as e:
        logger.debug(f"Could not resolve profiles watched by {viewer}: {e}")

    return profile_ids


# ── Stop Skipped Channels ────────────────────────────────────────────────────


def _skipped_stopping_key(channel_uuid) -> str:
    return SKIPPED_STOPPING_KEY.format(channel_uuid=channel_uuid)


def _release_channel_slot_now(redis_client, channel_uuid, hold_slot):
    """
    Release the slot of a channel about to be stopped right away, instead of at the end of
    its stop.

    Channel.release_stream() clears the assignment, so the later full stop does not release
    the slot a second time.
    """
    from django.core.exceptions import ValidationError
    from apps.channels.models import Channel

    try:
        channel = Channel.objects.filter(uuid=channel_uuid).first()
    except (ValidationError, ValueError):
        # Stream previews use a stream hash instead of a channel UUID; the stop releases those
        return
    if channel is None:
        return
    if not hold_slot:
        # This viewer already has its new slot; let a waiting request have this one
        redis_client.setex(NO_HOLD_KEY.format(channel_uuid=channel_uuid), 30, "1")
    channel.release_stream()


def _stop_channel(channel_uuid):
    """Background stop of a skipped or idle channel whose slot was already released."""
    from django.db import close_old_connections
    from core.utils import RedisClient
    from .services.channel_service import ChannelService

    try:
        ChannelService.stop_channel(channel_uuid)
    except Exception as e:
        logger.error(f"Probation: error stopping channel {channel_uuid}: {e}", exc_info=True)
    finally:
        try:
            # Requests waiting in wait_for_stop_to_finish() can start the channel again
            RedisClient.get_client().delete(_skipped_stopping_key(channel_uuid))
        except Exception as e:
            logger.debug(f"Could not clear stopping marker for channel {channel_uuid}: {e}")
        close_old_connections()


def wait_for_stop_to_finish(redis_client, channel_uuid) -> bool:
    """
    Called by stream_ts before it looks at the requested channel. When that channel is being
    stopped by this feature (a skipped or idle channel), wait until the stop is done, so the
    request starts the channel again.

    Without this, a player returning to a channel it just surfed past gets "Channel is
    stopping" (a 503 with a JSON body, which players such as TiviMate show as an error), or
    joins the channel just before the stop ends it. Other stops (dashboard, deletes) keep
    Dispatcharr's normal answer. Gives up after STOP_WAIT_SECONDS. Returns whether it waited.
    """
    channel_uuid = str(channel_uuid)
    try:
        if not redis_client or not in_use():
            return False
        if not redis_client.exists(_skipped_stopping_key(channel_uuid)):
            return False

        from .services.channel_service import ChannelService

        def stopping():
            return bool(
                redis_client.exists(_skipped_stopping_key(channel_uuid))
            ) or ChannelService.is_channel_teardown_active(channel_uuid)

        logger.info(
            f"Probation: channel {channel_uuid} is still being stopped after it was skipped; "
            f"waiting for the stop to finish before starting it again"
        )
        started = time.monotonic()
        while stopping():
            if time.monotonic() - started >= STOP_WAIT_SECONDS:
                logger.warning(
                    f"Probation: channel {channel_uuid} was still stopping after "
                    f"{STOP_WAIT_SECONDS}s"
                )
                return True
            gevent.sleep(STOP_WAIT_POLL_INTERVAL)
        logger.info(
            f"Probation: channel {channel_uuid} stopped after {time.monotonic() - started:.1f}s; "
            f"starting it again"
        )
        return True
    except Exception as e:
        logger.debug(f"Could not wait for channel {channel_uuid} to stop: {e}")
        return False


def _stop_channel_now(redis_client, channel_uuid, hold_slot=False):
    """Release a channel's slot at once and run the slower full stop in the background."""
    redis_client.setex(_skipped_stopping_key(channel_uuid), SKIPPED_STOPPING_TTL, "1")
    _release_channel_slot_now(redis_client, channel_uuid, hold_slot)
    gevent.spawn(_stop_channel, channel_uuid)


def stop_skipped_channels(redis_client, viewer, requested_channel_uuid, now=None, hold_slots=False):
    """
    Stop the channels this viewer surfed past, so their slots are free again.

    A channel counts as skipped when the viewer is its only client and joined it within the
    account's overlap window. Dispatcharr would otherwise keep it until it notices the
    player left, which fills every slot while surfing.

    stream_ts calls this once the requested channel has its slot (so switching can use the
    overlap slot and is not delayed), or earlier with hold_slots=True when no slot was free
    at all, in which case each released slot is held for this viewer. The slot is released
    at once; the rest of the stop, which waits for the provider connection to close, runs in
    the background. Returns the UUIDs of the channels being stopped.
    """
    if not is_viewer_request(viewer) or not viewer.identified or not redis_client:
        return []

    try:
        if not in_use() or not any_account_stops_skipped_channels():
            return []

        from apps.m3u.models import M3UAccountProfile

        now = now if now is not None else time.time()
        requested_channel_uuid = str(requested_channel_uuid)

        # (channel uuid, profile id, when this viewer first joined it)
        candidates = []
        for channel_uuid, profile_id in _active_channels(redis_client):
            if (
                profile_id is None
                or channel_uuid == requested_channel_uuid
                or redis_client.exists(_skipped_stopping_key(channel_uuid))
            ):
                continue

            joined = []
            for client in _channel_clients(redis_client, channel_uuid):
                try:
                    joined_at = float(client.get("connected_at"))
                except (TypeError, ValueError):
                    joined_at = None
                if joined_at is None or not _is_viewer(viewer, client):
                    # Someone else is watching too: never stop a shared channel
                    joined = []
                    break
                joined.append(joined_at)
            if joined:
                # The earliest join counts, so a player reconnecting to a channel it has
                # watched for a while does not make that channel look skipped.
                candidates.append((channel_uuid, profile_id, min(joined)))

        if not candidates:
            return []

        profiles = {
            profile.id: profile
            for profile in M3UAccountProfile.objects.select_related("m3u_account").filter(
                id__in={profile_id for _uuid, profile_id, _joined in candidates}
            )
        }

        stopped = []
        for channel_uuid, profile_id, joined_at in candidates:
            profile = profiles.get(profile_id)
            if profile is None:
                continue
            account = profile.m3u_account
            if not (
                account_allows_probation(account) and account_stops_skipped_channels(account)
            ):
                continue
            watched_for = now - joined_at
            if watched_for > account_probation_seconds(account):
                continue
            logger.info(
                f"Probation: stopping skipped channel {channel_uuid} (watched {watched_for:.1f}s) "
                f"for {viewer}, who requested channel {requested_channel_uuid}"
            )
            _stop_channel_now(redis_client, channel_uuid, hold_slots)
            stopped.append(channel_uuid)
        return stopped
    except Exception as e:
        logger.error(f"Probation: error stopping skipped channels for {viewer}: {e}", exc_info=True)
        return []


# ── Holding a released slot for its viewer ───────────────────────────────────


def _viewer_key(viewer) -> str:
    return f"{viewer.ip}|{viewer.user_id or 0}|{viewer.device_id or ''}"


def _held_slots_key(profile_id) -> str:
    return HELD_SLOTS_KEY.format(profile_id=profile_id)


def _held_login_slots_key(credential_key) -> str:
    return HELD_LOGIN_SLOTS_KEY.format(credential_key=credential_key)


def _parse_hold(value):
    """(expiry time, reference) from a hold's "expiry|reference" value."""
    expires_at, _sep, reference = _as_str(value).partition("|")
    try:
        return float(expires_at), reference or None
    except (TypeError, ValueError):
        return 0.0, None


def _stop_was_requested(redis_client, channel_uuid) -> bool:
    """Whether the channel, or a client on it, is being stopped on purpose."""
    if redis_client.exists(RedisKeys.channel_stopping(channel_uuid)):
        return True
    state = _as_str(
        redis_client.hget(RedisKeys.channel_metadata(channel_uuid), ChannelMetadataField.STATE)
    )
    if state == ChannelState.STOPPING:
        return True
    return any(
        redis_client.exists(RedisKeys.client_stop(channel_uuid, _as_str(client_id)))
        for client_id in redis_client.smembers(RedisKeys.clients(channel_uuid)) or ()
    )


def hold_slot_for_viewer(redis_client, channel_uuid, profile_id):
    """
    Called just before a channel releases its slot: keep the slot for the viewer that was
    watching it, for the account's overlap window.

    Many players close the old stream before requesting the next channel. Without a hold,
    a request that is waiting for a slot takes it in that gap and the switch fails. Only
    a channel with a single viewer is held, and only on accounts with the overlap enabled.
    A channel stopped on purpose (from the dashboard, deleted, removed by an M3U refresh,
    or a client disconnected from the dashboard) is not a channel switch and is not held.
    """
    channel_uuid = str(channel_uuid)
    try:
        if not in_use():
            return
        # Only needed while the channel runs (see CHANNEL_VIEWERS_KEY)
        redis_client.delete(_channel_viewers_key(channel_uuid))
        if redis_client.exists(NO_HOLD_KEY.format(channel_uuid=channel_uuid)):
            return
        if _stop_was_requested(redis_client, channel_uuid):
            return
        viewers = {
            _client_viewer(client)
            for client in _channel_clients(redis_client, channel_uuid)
            if client.get("ip_address")
        }
        if len(viewers) != 1:
            return
        (viewer,) = viewers
        if viewer.recording:
            return

        from apps.m3u.connection_pool import profile_credential_release_key
        from apps.m3u.models import M3UAccountProfile

        profile = M3UAccountProfile.objects.select_related("m3u_account").filter(id=profile_id).first()
        if profile is None or profile.max_streams == 0:
            return
        account = profile.m3u_account
        if not account_allows_probation(account):
            return
        if not viewer.identified and not account_allows_anonymous(account):
            return

        seconds = account_probation_seconds(account)
        expires_at = time.time() + seconds
        # Server Groups: the shared login counter this slot used, still known before release
        credential_key = _as_str(redis_client.get(profile_credential_release_key(profile.id)))
        key = _held_slots_key(profile.id)
        redis_client.hset(key, _viewer_key(viewer), f"{expires_at}|{credential_key or ''}")
        # Expired entries are ignored anyway; the TTL only cleans up the hash
        redis_client.expire(key, MAX_PROBATION_SECONDS + 5)
        if credential_key:
            login_key = _held_login_slots_key(credential_key)
            redis_client.hset(login_key, _viewer_key(viewer), f"{expires_at}|{profile.id}")
            redis_client.expire(login_key, MAX_PROBATION_SECONDS + 5)
        logger.info(
            f"Probation: holding the released slot on profile {profile.id} for {viewer} "
            f"for {seconds}s (channel {channel_uuid})"
        )
    except Exception as e:
        logger.debug(f"Could not hold slot for channel {channel_uuid}: {e}")


def slots_held_for_others(redis_client, profile, viewer=None, credential_key=None) -> int:
    """
    Slots held for other viewers switching channels: on the profile's own counter or, with
    credential_key, on a Server Group's shared login counter.

    apps.m3u.connection_pool counts them as taken, so every way of getting a slot (viewers,
    failover, stream changes, plugins, VOD, timeshift, previews) leaves them alone. The
    viewer a slot is held for can take it, and DVR recordings ignore holds: a scheduled
    recording must start on time.
    """
    if profile.max_streams == 0 or (viewer is not None and viewer.recording):
        return 0
    try:
        if not in_use():
            return 0
        key = _held_login_slots_key(credential_key) if credential_key else _held_slots_key(profile.id)
        own_key = _viewer_key(viewer) if viewer is not None else None
        now = time.time()
        held, expired = 0, []
        for viewer_key, value in (redis_client.hgetall(key) or {}).items():
            viewer_key = _as_str(viewer_key)
            if _parse_hold(value)[0] <= now:
                expired.append(viewer_key)
            elif viewer_key != own_key:
                held += 1
        if expired:
            redis_client.hdel(key, *expired)
        # Holds end with the window; one turned off meanwhile does not wait for that
        if held and not credential_key and not account_allows_probation(profile.m3u_account):
            return 0
    except Exception as e:
        logger.debug(f"Could not read held slots for profile {profile.id}: {e}")
        return 0
    if held:
        log_not_used(
            f"profile-{profile.id}",
            viewer,
            f"{held} slot(s) on profile {profile.id} held for a viewer switching channels",
        )
    return held


def _drop_hold(redis_client, viewer_key, profile_id) -> bool:
    """Remove a viewer's hold on a profile, and the matching hold on its shared login."""
    key = _held_slots_key(profile_id)
    value = redis_client.hget(key, viewer_key)
    if value is None:
        return False
    redis_client.hdel(key, viewer_key)
    credential_key = _parse_hold(value)[1]
    if credential_key:
        redis_client.hdel(_held_login_slots_key(credential_key), viewer_key)
    return True


def _drop_login_hold(redis_client, viewer_key, credential_key) -> bool:
    """Remove a viewer's hold on a shared login, and the matching hold on its profile."""
    key = _held_login_slots_key(credential_key)
    value = redis_client.hget(key, viewer_key)
    if value is None:
        return False
    redis_client.hdel(key, viewer_key)
    profile_id = _parse_hold(value)[1]
    if profile_id:
        redis_client.hdel(_held_slots_key(profile_id), viewer_key)
    return True


def take_held_slot(redis_client, profile, viewer):
    """Use up this viewer's hold after it got a slot on the profile (or its shared login) again."""
    if not is_viewer_request(viewer) or profile.max_streams == 0:
        return
    try:
        if not in_use():
            return
        from apps.m3u.connection_pool import profile_credential_release_key

        viewer_key = _viewer_key(viewer)
        taken = _drop_hold(redis_client, viewer_key, profile.id)
        credential_key = _as_str(redis_client.get(profile_credential_release_key(profile.id)))
        if credential_key:
            taken = _drop_login_hold(redis_client, viewer_key, credential_key) or taken
        if taken:
            logger.info(f"Probation: {viewer} took its held slot on profile {profile.id}")
    except Exception as e:
        logger.debug(f"Could not clear held slot for {viewer}: {e}")


# ── Account preference when switching ────────────────────────────────────────


def _last_profile_key(viewer) -> str:
    return LAST_PROFILE_KEY.format(viewer=_viewer_key(viewer))


def remember_viewer_profile(redis_client, viewer, profile, m3u_account):
    """Remember the profile a viewer was just assigned, on accounts with an account preference."""
    if not is_viewer_request(viewer) or not redis_client:
        return
    if not account_allows_probation(m3u_account):
        return
    if account_switch_preference(m3u_account) == ACCOUNT_PREFERENCE_ORDER:
        return
    try:
        redis_client.setex(_last_profile_key(viewer), LAST_PROFILE_TTL, profile.id)
    except Exception as e:
        logger.debug(f"Could not remember profile for {viewer}: {e}")


def reserve_sticky_slot(channel, redis_client, viewer):
    """
    Account preference "same": reserve a slot on the M3U profile this viewer is watching on
    or was just assigned, ahead of the channel's normal stream order.

    Only streams on accounts with probation_enabled and preference "same" are considered
    (anonymous viewers additionally need probation_allow_anonymous). A free slot on the
    preferred profile is used first; when that profile is full and the viewer is watching
    on it (its own old stream is still closing), the overlap slot is used instead of
    moving the viewer to another account. Returns get_stream()'s result tuple, or None
    to continue with normal selection.
    """
    if not is_viewer_request(viewer) or not redis_client or not in_use():
        return None

    from apps.m3u.connection_pool import reserve_profile_slot

    # Candidate (stream, profile) pairs limited to accounts that keep viewers
    candidates = _channel_candidates(
        channel,
        lambda account: account_allows_probation(account)
        and account_keeps_viewers(account)
        and (viewer.identified or account_allows_anonymous(account)),
    )
    if not candidates or channel.get_stream_profile().is_redirect():
        return None

    watched_profile_ids = find_profile_ids_watched_by(redis_client, viewer)
    preferred_profile_ids = list(watched_profile_ids)
    last_profile_id = _as_str(redis_client.get(_last_profile_key(viewer)))
    if last_profile_id and int(last_profile_id) not in preferred_profile_ids:
        preferred_profile_ids.append(int(last_profile_id))

    for preferred_profile_id in preferred_profile_ids:
        for stream, profile in candidates:
            if profile.id != preferred_profile_id:
                continue
            reserved, current_count, _failure_reason = reserve_profile_slot(
                profile, redis_client, viewer=viewer
            )
            on_overlap = False
            # Full only because of this viewer's own stream: overlap instead of switching
            # accounts. A profile the viewer merely left may be full with someone else.
            if not reserved and profile.max_streams > 0 and profile.id in watched_profile_ids:
                reserved, current_count, _failure_reason = reserve_profile_slot(
                    profile, redis_client, extra_capacity=PROBATION_EXTRA_CAPACITY, viewer=viewer
                )
                on_overlap = reserved
            if not reserved:
                continue
            take_held_slot(redis_client, profile, viewer)

            redis_client.set(f"channel_stream:{channel.id}", stream.id)
            redis_client.set(f"stream_profile:{stream.id}", profile.id)
            window = ""
            if on_overlap:
                seconds = account_probation_seconds(stream.m3u_account)
                mark_probation(redis_client, channel, stream.id, profile.id, seconds)
                window = f" on overlap slot, window {seconds}s"
            remember_viewer_profile(redis_client, viewer, profile, stream.m3u_account)
            logger.info(
                f"Probation: keeping {viewer} on profile {profile.id} ({profile.name}) for "
                f"channel {channel.uuid}, stream {stream.id} "
                f"({current_count}/{profile.max_streams}){window} (stay on same account)"
            )
            return stream.id, profile.id, None, True
    return None


def _channel_candidates(channel, account_filter=None):
    """
    (stream, profile) pairs in channel order with the default profile first, like
    get_stream(). Profiles are only loaded for accounts that pass account_filter.
    """
    candidates = []
    for stream in channel.streams.select_related("m3u_account").order_by("channelstream__order"):
        account = stream.m3u_account
        if not account or not account.is_active:
            continue
        if account_filter is not None and not account_filter(account):
            continue
        profiles = sorted(account.profiles.filter(is_active=True), key=lambda p: not p.is_default)
        candidates.extend((stream, profile) for profile in profiles)
    return candidates


def _release_viewer_holds(redis_client, viewer, profile_ids):
    for profile_id in profile_ids:
        _drop_hold(redis_client, _viewer_key(viewer), profile_id)


def reserve_alternate_slot(channel, redis_client, viewer):
    """
    Account preference "alternate": start the viewer's next channel on a free slot on another
    profile than the one it is watching on, was just assigned, or has a held slot on.

    Players often close the old stream just before requesting the next channel, so the old
    account is free again and the normal stream order would pick it every time. Starting on
    another account instead avoids waiting for the provider to close the old connection.
    Applies when the profile being left is on an account with probation_enabled and
    preference "alternate"; the new profile must be on an account with probation_enabled
    too, and custom streams are never used. Only free slots are used here; when no other
    profile has one, None is returned and normal selection (held slot, overlap) continues.
    """
    if not is_viewer_request(viewer) or not redis_client or not in_use():
        return None

    # Nothing to do (and nothing to read from Redis) unless an account here alternates.
    # Accounts without the overlap are never moved to, so their profiles are not loaded.
    candidates = _channel_candidates(channel, account_allows_probation)
    accounts = {profile.id: stream.m3u_account for stream, profile in candidates}
    if not any(account_alternates_viewers(account) for account in accounts.values()):
        return None

    left_profile_ids = set(find_profile_ids_watched_by(redis_client, viewer))
    last_profile_id = _as_str(redis_client.get(_last_profile_key(viewer)))
    if last_profile_id:
        left_profile_ids.add(int(last_profile_id))
    viewer_key = _viewer_key(viewer)
    left_profile_ids.update(
        profile_id
        for profile_id in accounts
        if redis_client.hget(_held_slots_key(profile_id), viewer_key)
    )
    left_profile_ids = {
        profile_id
        for profile_id in left_profile_ids
        if profile_id in accounts
        and account_allows_probation(accounts[profile_id])
        and account_alternates_viewers(accounts[profile_id])
        and (viewer.identified or account_allows_anonymous(accounts[profile_id]))
    }
    if not left_profile_ids or channel.get_stream_profile().is_redirect():
        return None

    from apps.m3u.connection_pool import reserve_profile_slot

    for stream, profile in candidates:
        account = stream.m3u_account
        # Only move between provider accounts that use the overlap themselves (candidates are
        # limited to those). Custom streams (for example a fallback slate added by a plugin on
        # the unlimited "custom" account) are always free, so they would be picked every time.
        if profile.id in left_profile_ids or stream.is_custom:
            continue
        reserved, current_count, _failure_reason = reserve_profile_slot(
            profile, redis_client, viewer=viewer
        )
        if not reserved:
            continue

        redis_client.set(f"channel_stream:{channel.id}", stream.id)
        redis_client.set(f"stream_profile:{stream.id}", profile.id)
        take_held_slot(redis_client, profile, viewer)
        # The viewer does not need the slot it left any more; let others have it
        _release_viewer_holds(redis_client, viewer, left_profile_ids)
        remember_viewer_profile(redis_client, viewer, profile, account)
        logger.info(
            f"Probation: moving {viewer} to profile {profile.id} ({profile.name}) for channel "
            f"{channel.uuid}, stream {stream.id} ({current_count}/{profile.max_streams}), "
            f"leaving profile(s) {sorted(left_profile_ids)} (use another account)"
        )
        return stream.id, profile.id, None, True
    return None


# ── Overlap: reserve, record, resolve, monitor ───────────────────────────────


def reserve_overlap_slot(channel, redis_client, viewer, full_candidates):
    """
    Called by Channel.get_stream() once the profiles it tried are full: a viewer already
    watching on one of them is probably switching channels, so start its new channel now on
    that profile's temporary slot (one past max_streams). Other viewers get the normal limit
    error. Returns get_stream()'s result tuple, or None when the overlap does not apply.

    full_candidates are the (stream, profile) pairs found full, in channel order. get_stream()
    calls this before the first custom stream as well as after all streams, so an unlimited
    fallback stream at the end of a channel does not replace a channel switch.
    """
    if not is_viewer_request(viewer) or not full_candidates:
        return None

    from apps.m3u.connection_pool import reserve_profile_slot

    # Unlimited profiles (max_streams 0) are never full. Accounts without the overlap enabled
    # keep the existing behavior exactly: nothing below runs and nothing is logged for them.
    candidates = [
        (stream, profile)
        for stream, profile in full_candidates
        if profile.max_streams > 0 and account_allows_probation(stream.m3u_account)
    ]
    if candidates and not viewer.identified:
        # Anonymous viewers can only be matched by IP; accounts opt in separately.
        candidates = [
            (stream, profile)
            for stream, profile in candidates
            if account_allows_anonymous(stream.m3u_account)
        ]
        if not candidates:
            log_not_used(
                channel.uuid,
                viewer,
                f"{viewer} has no user or device ID and no full account allows anonymous "
                f"connections",
            )
    if not candidates:
        return None
    if channel.get_stream_profile().is_redirect():
        # Redirects hand the provider URL to the player and release the slot, so there is
        # no proxied stream whose overlap could be resolved.
        log_not_used(channel.uuid, viewer, "redirect stream profile")
        return None

    watched_profile_ids = find_profile_ids_watched_by(redis_client, viewer)
    watched_candidates = [
        (stream, profile) for stream, profile in candidates if profile.id in watched_profile_ids
    ]
    if not watched_candidates:
        log_not_used(
            channel.uuid, viewer, f"{viewer} is not watching on an account that allows the overlap"
        )
    for stream, profile in watched_candidates:
        # Same atomic INCR-first reservation, allowed one slot past the limit. Fails when
        # another switch already uses this profile's extra slot.
        reserved, current_count, _failure_reason = reserve_profile_slot(
            profile, redis_client, extra_capacity=PROBATION_EXTRA_CAPACITY, viewer=viewer
        )
        if not reserved:
            log_not_used(
                channel.uuid, viewer, f"overlap slot on profile {profile.id} is already in use"
            )
            continue
        # Assigned like a normal slot; the probation record makes stream_ts start a monitor
        # that confirms, moves or stops it.
        redis_client.set(f"channel_stream:{channel.id}", stream.id)
        redis_client.set(f"stream_profile:{stream.id}", profile.id)
        seconds = account_probation_seconds(stream.m3u_account)
        mark_probation(redis_client, channel, stream.id, profile.id, seconds)
        take_held_slot(redis_client, profile, viewer)
        remember_viewer_profile(redis_client, viewer, profile, stream.m3u_account)
        logger.info(
            f"Probation: channel {channel.uuid} assigned stream {stream.id} profile "
            f"{profile.id} ({profile.name}) on overlap slot "
            f"({current_count}/{profile.max_streams}) for {viewer}, window {seconds}s"
        )
        return stream.id, profile.id, None, True
    return None


def _monitor_lease_key(channel_uuid) -> str:
    return MONITOR_LEASE_KEY.format(channel_uuid=channel_uuid)


def mark_probation(redis_client, channel, stream_id, profile_id, seconds):
    """
    Record that a channel's newly reserved slot is over its profile limit.

    stream_id and profile_id let resolve_probation() notice when the channel stopped or
    failover moved it, which ends the probation without further action.
    """
    now = time.time()
    channel_uuid = str(channel.uuid)
    key = probation_key(channel_uuid)
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
    # forever if no monitor ever runs.
    redis_client.expire(key, int(seconds) + 120)
    redis_client.sadd(PENDING_PROBATIONS_KEY, channel_uuid)
    # stream_ts starts the monitor once the channel is initialised; until then no other
    # worker should take the probation over (see recover_unmonitored_probations)
    redis_client.setex(_monitor_lease_key(channel_uuid), MONITOR_LEASE_TTL, "starting")


def has_pending_probation(redis_client, channel_uuid) -> bool:
    try:
        return bool(redis_client.exists(probation_key(channel_uuid)))
    except Exception:
        return False


def _clear(redis_client, channel_uuid):
    redis_client.delete(probation_key(channel_uuid), _monitor_lease_key(channel_uuid))
    redis_client.srem(PENDING_PROBATIONS_KEY, channel_uuid)


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


def _stop_idle_channels(redis_client, profile_id, probation_channel_uuid) -> list:
    """
    Stop the channels on a profile that nobody watches any more and only run on because of
    the Channel Shutdown Delay. Returns their UUIDs.

    With a shutdown delay the old channel of a switch keeps its slot for the whole delay, so
    the profile would stay over its limit and the switch could never be confirmed. Nobody is
    watching those channels, so they are stopped instead of keeping the provider over its
    limit.
    """
    stopped = []
    for channel_uuid, channel_profile_id in _active_channels(redis_client):
        if channel_profile_id != profile_id or channel_uuid == probation_channel_uuid:
            continue
        if not _waiting_for_shutdown(redis_client, channel_uuid):
            continue
        logger.info(
            f"Probation: stopping channel {channel_uuid} on profile {profile_id}: no viewers "
            f"left, it only waits for the Channel Shutdown Delay (overlap on channel "
            f"{probation_channel_uuid})"
        )
        _stop_channel_now(redis_client, channel_uuid)
        stopped.append(channel_uuid)
    return stopped


def _migrate_to_free_profile(channel, current_stream_id, current_profile_id, redis_client) -> bool:
    """
    Move a probation channel to a stream on another profile that has capacity.

    Provider streams are tried first. Custom streams come last: a fallback slate such as the
    could-not-dispatch plugin's is always free, and it is where the channel would have ended
    up without the overlap anyway, which beats stopping it.
    """
    from apps.m3u.connection_pool import pool_has_capacity_for_profile
    from .services.channel_service import ChannelService
    from .url_utils import get_stream_info_for_switch

    channel_uuid = str(channel.uuid)
    # Same order and default-profile-first rule as Channel.get_stream().
    streams = list(channel.streams.select_related("m3u_account").order_by("channelstream__order"))
    streams.sort(key=lambda stream: stream.is_custom)
    for stream in streams:
        # Switching within the same stream row does not move profile counters
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
            # Also leaves slots held for viewers switching channels alone
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

    If moving or stopping the channel raises, the record is kept: the monitor ends, and
    recover_unmonitored_probations() retries it on a worker, up to MAX_RESOLVE_ATTEMPTS times.
    """
    from core.utils import RedisClient
    from apps.channels.models import Channel
    from apps.m3u.models import M3UAccountProfile

    redis_client = redis_client or RedisClient.get_client()
    now = now if now is not None else time.time()
    channel_uuid = str(channel_uuid)
    key = probation_key(channel_uuid)

    record = redis_client.hgetall(key) or {}
    record = {_as_str(k): _as_str(v) for k, v in record.items()}
    if not record:
        redis_client.srem(PENDING_PROBATIONS_KEY, channel_uuid)
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

    within_limits = _profile_within_limits(profile, redis_client)
    if not within_limits and _stop_idle_channels(redis_client, profile.id, channel_uuid):
        # Their slots were released at once, so the switch can be confirmed right away
        within_limits = _profile_within_limits(profile, redis_client)

    if within_limits:
        logger.info(
            f"Probation: channel {channel_uuid} confirmed on profile {profile_id} "
            f"after {now - float(record.get('started_at', now)):.1f}s"
        )
        _clear(redis_client, channel_uuid)
        return CONFIRMED

    if now < deadline:
        return PENDING

    # Window expired while still over the limit: this was not a channel switch.
    attempts = int(redis_client.hincrby(key, "attempts", 1))
    if attempts > MAX_RESOLVE_ATTEMPTS:
        logger.error(
            f"Probation: could not move or stop channel {channel_uuid} after "
            f"{MAX_RESOLVE_ATTEMPTS} attempts; profile {profile_id} may stay over its limit "
            f"until a stream on it ends"
        )
        _clear(redis_client, channel_uuid)
        return FAILED

    channel = Channel.objects.filter(id=channel_id).first()
    try:
        migrated = channel is not None and _migrate_to_free_profile(
            channel, stream_id, profile_id, redis_client
        )
    except Exception as e:
        logger.error(f"Probation: error moving channel {channel_uuid}: {e}", exc_info=True)
        migrated = False
    if migrated:
        _clear(redis_client, channel_uuid)
        return MIGRATED

    from .services.channel_service import ChannelService

    # Only the probation channel is stopped; streams that were already playing stay.
    # It was not a switch, so its slot is not held for its viewer.
    redis_client.setex(NO_HOLD_KEY.format(channel_uuid=channel_uuid), 30, "1")
    logger.warning(
        f"Probation: no stream ended on profile {profile_id} within the overlap window "
        f"and no other profile has capacity; stopping channel {channel_uuid}"
    )
    ChannelService.stop_channel(channel_uuid)
    _clear(redis_client, channel_uuid)
    return STOPPED


def _monitor(channel_uuid, token):
    """
    Poll resolve_probation() until the probation is no longer pending.

    The monitor renews its lease on every check. It stops when another worker has taken the
    probation over, which only happens after this monitor failed to renew the lease in time.
    """
    from django.db import close_old_connections
    from core.utils import RedisClient

    lease_key = _monitor_lease_key(channel_uuid)
    # Close DB connections after each check: a long-lived greenlet would otherwise hold a
    # pooled connection for the whole window.
    try:
        while True:
            try:
                redis_client = RedisClient.get_client()
                owner = _as_str(redis_client.get(lease_key))
                if owner and owner != token:
                    logger.info(
                        f"Probation: another worker took over the overlap check for channel "
                        f"{channel_uuid}"
                    )
                    return
                redis_client.setex(lease_key, MONITOR_LEASE_TTL, token)
                outcome = resolve_probation(channel_uuid, redis_client)
            except Exception as e:
                # The lease runs out and another check picks the probation up again
                logger.error(f"Probation: error resolving channel {channel_uuid}: {e}", exc_info=True)
                return
            finally:
                close_old_connections()
            if outcome != PENDING:
                return
            gevent.sleep(MONITOR_POLL_INTERVAL)
    finally:
        close_old_connections()


def start_probation_monitor(channel_uuid, redis_client=None):
    """Resolve a channel's probation in the background, on this worker."""
    from core.utils import RedisClient

    channel_uuid = str(channel_uuid)
    token = secrets.token_hex(8)
    redis_client = redis_client or RedisClient.get_client()
    redis_client.setex(_monitor_lease_key(channel_uuid), MONITOR_LEASE_TTL, token)
    logger.info(f"Probation: watching channel {channel_uuid} until its overlap window ends")
    return gevent.spawn(_monitor, channel_uuid, token)


def recover_unmonitored_probations(redis_client):
    """
    Resume overlap checks that no worker is running any more, for example because the worker
    running the monitor restarted. Without this the extra connection would stay open until
    some stream on that profile ends.

    Called from every worker's proxy cleanup loop; costs one Redis call while nothing is
    pending. A monitor that is alive keeps its lease, so only one worker takes a probation over.
    """
    try:
        if not redis_client or not redis_client.scard(PENDING_PROBATIONS_KEY):
            return
        for channel_uuid in redis_client.smembers(PENDING_PROBATIONS_KEY) or ():
            channel_uuid = _as_str(channel_uuid)
            if not redis_client.exists(probation_key(channel_uuid)):
                redis_client.srem(PENDING_PROBATIONS_KEY, channel_uuid)
                continue
            token = secrets.token_hex(8)
            if redis_client.set(
                _monitor_lease_key(channel_uuid), token, nx=True, ex=MONITOR_LEASE_TTL
            ):
                logger.warning(
                    f"Probation: the overlap check for channel {channel_uuid} was not running "
                    f"(worker restarted?); resuming it on this worker"
                )
                gevent.spawn(_monitor, channel_uuid, token)
    except Exception as e:
        logger.debug(f"Could not recover overlap checks: {e}")
