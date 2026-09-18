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

A viewer is recognised by its Dispatcharr user (Xtream login), or, on the account's LAN
Subnets, by its IP address plus its player app ("probation_lan_subnets"): on a local network
every device has its own address. A device outside those subnets therefore needs its own
Viewers with neither (HDHomeRun, and a media server that cannot say which of its devices is
asking) take no part in it at all: acting on a viewer that cannot be told apart is how one
person's channel gets stopped for another. Streams that were already playing, or that someone
else also watches, are never stopped.

"Probation" is the internal name; the UI and logs say overlap. See
docs/channel-switch-overlap.md for the full design.
"""

import ipaddress
import logging
import re
import secrets
import socket
import time
from dataclasses import dataclass, field
from functools import lru_cache, wraps
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
# Surfing Delay ("probation_surf_delay_ms"): a viewer that switches again within the overlap
# window of its previous channel request waits this long before Dispatcharr connects to the
# provider, so a channel it passes in the meantime is never requested there.
DEFAULT_SURF_DELAY_MS = 500
MAX_SURF_DELAY_MS = 2000
# A viewer's latest channel request: "request time|token|channel uuid"
LAST_REQUEST_KEY = "live:probation:last_request:{viewer}"
# A provider that closes a new connection before sending any data is almost always refusing
# it (account full, or blocked for too many connections). Dispatcharr retries after 0.25 s and
# 0.5 s; on accounts with the overlap enabled the retries wait this long per attempt instead.
REFUSAL_RETRY_SECONDS = 1.5
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
# Recent switches for the Channel Switch Overlap page in the settings: a sorted set of event
# ids by time, and one short-lived hash per event (see record_event).
EVENTS_KEY = "live:probation:events"
EVENT_KEY = "live:probation:event:{event_id}"
# Short-lived on purpose: enough to see whether the feature is doing its job, never a log.
# How long switches are kept can be changed on the page itself (see event_ttl); the number of
# switches is capped so a long window cannot fill Redis.
EVENTS_KEPT = 200
EVENT_TTL = 30 * 60
EVENT_TTL_CHOICES = (30 * 60, 2 * 3600, 6 * 3600, 24 * 3600)
EVENT_TTL_KEY = "live:probation:events_keep"

# Slot assignments (channel_stream:<id>) whose channel has no live proxy keys at all: hash of
# assignment key -> first time it was seen that way. Released after ABANDONED_SLOT_GRACE
# seconds (see release_abandoned_slots). One worker checks every SWEEP_INTERVAL seconds.
# Unlike everything else in this module this does not depend on the overlap being enabled:
# it fixes accounts that stay "full" after a restart for every Dispatcharr setup.
ABANDONED_SLOTS_KEY = "live:probation:abandoned_slots"
ABANDONED_SLOT_GRACE = 60
SWEEP_LOCK_KEY = "live:probation:sweep_lock"
SWEEP_INTERVAL = 30

# Media servers stream on behalf of all of their viewers, so their requests say nothing about
# a single device. Matched against the default User-Agents (for example
# "Jellyfin-Server/10.10.7", "Emby/4.8.10.0", "PlexMediaServer/1.41.0").
# Jellyfin, Emby and Plex by name, and "Lavf/..." (ffmpeg's own User-Agent): Plex pulls a
# tuner channel with ffmpeg, so that is a server fetching on behalf of its viewers, never one
# player we could tell apart. A media server is also recognised by its address (see
# media_servers.server_hosts), which is what a configured server is really known by.
_MEDIA_SERVER_RE = re.compile(r"jellyfin|emby|plex|lavf", re.IGNORECASE)
# DVR recordings request channels through the proxy with this User-Agent
# (core.utils.dispatcharr_dvr_user_agent). They are not viewers switching channels.
_RECORDING_USER_AGENT_PREFIX = "Dispatcharr-DVR"

# Whether any M3U account has the overlap enabled, cached for all workers and cleared when
# an account is saved or deleted. Everything here checks it first, so Dispatcharr does no
# extra work while the feature is not used.
IN_USE_CACHE_KEY = "live:probation:in_use"
IN_USE_CACHE_TTL = 60
# The same for LAN Device Tracking ("probation_lan_subnets"), cleared together with it.
LAN_TRACKING_CACHE_KEY = "live:probation:lan_tracking"

# Version numbers in a User-Agent, removed to recognise the app across updates
# ("TiviMate/5.1.6 (Android 12)" -> "TiviMate/ (Android )")
_VERSION_RE = re.compile(r"\d+(?:[._]\d+)*")

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
    # A DVR recording: never uses the overlap, account preferences or held slots
    recording: bool = False
    # The player app without version numbers (see app_name), used on an account's LAN subnets.
    # Left out of comparison on purpose: whether two requests are the same viewer depends on
    # the account (its LAN subnets decide whether the app counts), so identity_key() answers
    # that, not ==. Comparing Viewers is only used where the account is not known.
    app: Optional[str] = field(default=None, compare=False)
    # The device behind a media server, when its server could say which one (see
    # media_servers.sole_device). A media server asks on behalf of its viewers, so without
    # this it is one anonymous viewer for all of them.
    server_device: Optional[str] = None


def is_viewer_request(viewer) -> bool:
    """Whether a request can take part in the overlap: a live viewer, not a recording."""
    return viewer is not None and not viewer.recording


def probation_key(channel_uuid) -> str:
    return PROBATION_KEY.format(channel_uuid=channel_uuid)


# ── Logging ──────────────────────────────────────────────────────────────────

# (channel uuid, viewer, reason) -> monotonic time last logged, per worker process
_not_used_logged = {}


def log_not_used(channel_uuid, viewer, reason):
    """
    Log why the overlap was not used, without repeating it on every retry attempt.

    Reached from slots_held_for_others(), which apps.m3u.connection_pool calls on every
    capacity check and every reservation, so this is on the path of a stream request: it asks
    for a Redis client, which can block or raise while one is being made. Wrapped for that
    reason -- saying why the overlap did nothing must never be the reason a stream does not
    start.
    """
    try:
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
        if not str(channel_uuid).startswith("profile-"):
            from core.utils import RedisClient

            record_event(
                RedisClient.get_client(), viewer, "not used", channel=channel_uuid, result=reason
            )
    except Exception as e:
        logger.debug(f"Could not log why the overlap was not used: {e}")


def _never_breaks_a_stream(what):
    """
    Wrap one of the three entry points Channel.get_stream() calls directly.

    Everything else in this module catches its own failures; these three did not, and they do
    database queries and Redis calls on the path of a stream request. A failure here must
    leave the request exactly as it would have been without the feature -- no slot, no
    overlap, the stream still starts -- rather than becoming a 500.
    """

    def wrap(function):
        @wraps(function)
        def guarded(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except Exception as e:
                logger.error(f"Probation: could not {what}: {e}", exc_info=True)
                return None

        return guarded

    return wrap


# ── Recent switches (settings page) ──────────────────────────────────────────


def _event_key(event_id) -> str:
    return EVENT_KEY.format(event_id=event_id)


def event_ttl(redis_client) -> int:
    """How long switches are kept, as chosen on the page (EVENT_TTL when not set)."""
    try:
        chosen = int(_as_str(redis_client.get(EVENT_TTL_KEY)) or 0)
    except (TypeError, ValueError):
        chosen = 0
    return chosen if chosen in EVENT_TTL_CHOICES else EVENT_TTL


def set_event_ttl(redis_client, seconds) -> int:
    """Remember how long switches are kept; anything but a known choice is refused."""
    seconds = int(seconds)
    if seconds not in EVENT_TTL_CHOICES:
        raise ValueError(f"{seconds} is not one of {EVENT_TTL_CHOICES}")
    redis_client.set(EVENT_TTL_KEY, seconds)
    return seconds


# A viewer's previous channel, so an ordinary switch can be reported like any other
PREVIOUS_CHANNEL_KEY = "live:probation:previous:{viewer}"
PREVIOUS_CHANNEL_TTL = 30 * 60


def record_switch(redis_client, viewer, channel_uuid):
    """
    Report a switch the overlap did not have to do anything about.

    Most switches are uneventful: a slot was free, so the new channel simply started. Without
    this the page only ever shows the switches that went wrong, which makes a working setup
    look like a page that is broken.

    Called once the channel has its stream. Never raises, and does nothing while the overlap
    is switched off everywhere.
    """
    if not redis_client or not is_viewer_request(viewer) or not channel_uuid:
        return
    try:
        if not in_use():
            return
        channel_uuid = str(channel_uuid)
        key = PREVIOUS_CHANNEL_KEY.format(viewer=_player_key(viewer))
        previous = _as_str(redis_client.get(key))
        redis_client.setex(key, PREVIOUS_CHANNEL_TTL, channel_uuid)
        if not previous or previous == channel_uuid:
            # Its first channel, or the same one again: nothing was switched
            return
        if redis_client.exists(_event_key_for_channel(channel_uuid)):
            # The overlap already reported this one, with more to say about it
            return

        if not may_be_identified(viewer):
            record_event(
                redis_client,
                viewer,
                "not used",
                from_channel=previous,
                channel=channel_uuid,
                result=why_not_identified(viewer),
            )
            return
        record_event(
            redis_client,
            viewer,
            "switched",
            from_channel=previous,
            channel=channel_uuid,
            result="a slot was free",
        )
    except Exception as e:
        logger.debug(f"Could not record the switch to {channel_uuid}: {e}")


def _event_key_for_channel(channel_uuid) -> str:
    """
    A marker saying the overlap already reported this channel, so a switch is not reported
    twice: once by whatever the overlap did and once as an ordinary one.
    """
    return f"live:probation:reported:{channel_uuid}"


def record_event(redis_client, viewer, action, **fields):
    """
    Remember one decision for the Channel Switch Overlap page: who switched, from and to
    which channel, and what happened. Returns the event id, which update_event() uses to fill
    in the result once it is known.

    Kept deliberately small: only the last EVENTS_KEPT switches, and only for as long as the
    page is set to keep them (see event_ttl).
    Never lets a failure reach the stream request.
    """
    if not redis_client:
        return None
    try:
        now = time.time()
        event_id = secrets.token_hex(6)
        event = {
            "time": str(now),
            "ip": (viewer.ip if viewer else "") or "",
            "user_id": str((viewer.user_id if viewer else None) or ""),
            "app": (viewer.app if viewer else "") or "",
            # A media server device, so the page can say who was watching (see device_name)
            "server_device": (viewer.server_device if viewer else "") or "",
            "action": action,
        }
        event.update({key: str(value) for key, value in fields.items() if value is not None})
        redis_client.hset(_event_key(event_id), mapping=event)
        ttl = event_ttl(redis_client)
        redis_client.expire(_event_key(event_id), ttl)
        if fields.get("channel") and action != "switched":
            # So an ordinary switch is not also reported for a channel the overlap acted on
            redis_client.setex(_event_key_for_channel(fields["channel"]), 60, "1")
        redis_client.zadd(EVENTS_KEY, {event_id: now})
        redis_client.zremrangebyrank(EVENTS_KEY, 0, -(EVENTS_KEPT + 1))
        redis_client.zremrangebyscore(EVENTS_KEY, "-inf", now - ttl)
        redis_client.expire(EVENTS_KEY, ttl)
        return event_id
    except Exception as e:
        logger.debug(f"Could not record an overlap event: {e}")
        return None


def update_event(redis_client, event_id, **fields):
    """Fill in the result of an earlier event (confirmed, moved, stopped, ...)."""
    if not redis_client or not event_id:
        return
    try:
        if redis_client.exists(_event_key(event_id)):
            redis_client.hset(
                _event_key(event_id),
                mapping={key: str(value) for key, value in fields.items() if value is not None},
            )
    except Exception as e:
        logger.debug(f"Could not update overlap event {event_id}: {e}")


def recent_events(redis_client):
    """The last switches, newest first, for the settings page."""
    events = []
    try:
        event_ids = redis_client.zrevrange(EVENTS_KEY, 0, EVENTS_KEPT - 1) or ()
        for event_id in event_ids:
            event = redis_client.hgetall(_event_key(_as_str(event_id))) or {}
            if event:
                events.append({_as_str(k): _as_str(v) for k, v in event.items()})
    except Exception as e:
        logger.debug(f"Could not read overlap events: {e}")
    return events


# ── Account settings ─────────────────────────────────────────────────────────


def _account_props(m3u_account):
    from core.utils import custom_properties_as_dict

    return custom_properties_as_dict(getattr(m3u_account, "custom_properties", None))


def account_allows_probation(m3u_account) -> bool:
    # Strict "is True": custom_properties is free-form JSON and must opt in explicitly.
    return _account_props(m3u_account).get("probation_enabled") is True


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


def account_tracks_lan_devices(m3u_account) -> bool:
    """LAN Device Tracking is on when the account has LAN Subnets; an empty list is off."""
    return account_allows_probation(m3u_account) and bool(account_lan_subnets(m3u_account))


@lru_cache(maxsize=256)
def _parse_subnets(subnets):
    networks = []
    for subnet in subnets:
        try:
            network = ipaddress.ip_network(str(subnet).strip(), strict=False)
        except ValueError:
            continue
        # Only local networks: a public address says nothing about a single device
        if network.is_private:
            networks.append(network)
    return tuple(networks)


def account_lan_subnets(m3u_account):
    """The account's LAN Device Tracking subnets as ip_network objects (invalid entries skipped)."""
    subnets = _account_props(m3u_account).get("probation_lan_subnets") or ()
    if isinstance(subnets, str):
        subnets = re.split(r"[,\s]+", subnets)
    return _parse_subnets(tuple(s for s in subnets if s))


def parse_lan_subnets(value):
    """
    Normalise LAN subnets from the API: a list, or a comma or space separated string (form
    uploads send lists that way). Raises ValueError for an invalid or non-local subnet.
    """
    if isinstance(value, str):
        value = re.split(r"[,\s]+", value)
    subnets = []
    for entry in value or ():
        entry = str(entry).strip()
        if not entry:
            continue
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            raise ValueError(f"{entry} is not a valid IP address or subnet")
        if not network.is_private:
            raise ValueError(f"{entry} is not a local network address")
        if str(network) not in subnets:
            subnets.append(str(network))
    return subnets


@lru_cache(maxsize=1)
def suggested_lan_subnet():
    """
    A /24 around the address Dispatcharr uses to reach the network, as a starting point for
    the subnet field, or None. Not suggested for Docker networks (172.16.0.0/12), where that
    address is not the address of the LAN the players are on.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            # UDP connect sends nothing; it only picks the outgoing interface
            probe.connect(("10.255.255.255", 1))
            address = ipaddress.ip_address(probe.getsockname()[0])
    except OSError:
        return None
    if (
        not address.is_private
        or address.is_loopback
        or address in ipaddress.ip_network("172.16.0.0/12")
    ):
        return None
    return str(ipaddress.ip_network(f"{address}/24", strict=False))


def account_surf_delay_seconds(m3u_account) -> float:
    try:
        delay_ms = int(
            _account_props(m3u_account).get("probation_surf_delay_ms", DEFAULT_SURF_DELAY_MS)
        )
    except (TypeError, ValueError):
        delay_ms = DEFAULT_SURF_DELAY_MS
    return min(max(delay_ms, 0), MAX_SURF_DELAY_MS) / 1000


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


def any_account_tracks_lan_devices() -> bool:
    from apps.m3u.models import M3UAccount

    return any(
        account_lan_subnets(account)
        for account in M3UAccount.objects.filter(
            is_active=True, custom_properties__probation_enabled=True
        )
    )


def _cached_flag(cache_key, compute, description) -> bool:
    """
    Whether a setting is in use anywhere, asked at most once a minute per worker.

    Every entry point checks one of these first, so it has to be cheap: without the cache each
    would be a database query on the path of a stream request. The cost is a stale answer for
    up to a minute, which the post_save/post_delete receivers below cut short whenever an
    account actually changes. A failure is treated as "not in use" and not cached, so a
    database hiccup makes Dispatcharr behave exactly as it does without the feature rather
    than breaking a request.
    """
    from django.core.cache import cache

    try:
        cached = cache.get(cache_key)
    except Exception:
        cached = None
    if cached is None:
        try:
            cached = int(compute())
        except Exception as e:
            # Never let this break a stream request; ask again next time
            logger.debug(f"Could not check whether {description} is in use: {e}")
            return False
        try:
            cache.set(cache_key, cached, IN_USE_CACHE_TTL)
        except Exception:
            pass
    return bool(cached)


def in_use() -> bool:
    """Whether any active M3U account has the overlap enabled (cached, see IN_USE_CACHE_KEY)."""
    return _cached_flag(IN_USE_CACHE_KEY, any_account_allows_probation, "Channel Switch Overlap")


def lan_tracking_in_use() -> bool:
    """Whether any active account uses LAN Device Tracking (cached like in_use())."""
    return in_use() and _cached_flag(
        LAN_TRACKING_CACHE_KEY, any_account_tracks_lan_devices, "LAN Device Tracking"
    )


def forget_in_use(**_kwargs):
    """Signal receiver: an account changed, so read the setting again on next use."""
    from django.core.cache import cache

    try:
        cache.delete_many([IN_USE_CACHE_KEY, LAN_TRACKING_CACHE_KEY])
    except Exception as e:
        logger.debug(f"Could not clear {IN_USE_CACHE_KEY}: {e}")


post_save.connect(forget_in_use, sender="m3u.M3UAccount", dispatch_uid="probation_in_use_save")
post_delete.connect(forget_in_use, sender="m3u.M3UAccount", dispatch_uid="probation_in_use_delete")


# ── Recognising the player of a request ──────────────────────────────────────


def is_media_server(user_agent, ip=None) -> bool:
    """A media server by name, or by the address of a server configured in Media Servers."""
    if user_agent and _MEDIA_SERVER_RE.search(user_agent):
        return True
    if not ip:
        return False
    try:
        from . import media_servers

        return str(ip).lower() in media_servers.server_hosts()
    except Exception as e:
        logger.debug(f"Could not check whether {ip} is a media server: {e}")
        return False


def is_recording(user_agent) -> bool:
    return bool(user_agent and user_agent.startswith(_RECORDING_USER_AGENT_PREFIX))


def app_name(user_agent, ip=None):
    """
    The player app from a User-Agent without version numbers, so an app update does not
    look like another device. None for media servers and recordings, which LAN Device
    Tracking never applies to.
    """
    if not user_agent or is_media_server(user_agent, ip) or is_recording(user_agent):
        return None
    return re.sub(r"\s+", " ", _VERSION_RE.sub("", user_agent)).strip() or None


def viewer_from_request(request, user, client_ip, redis_client=None):
    """Viewer identity for a live stream request, or None without a client IP."""
    if not client_ip:
        return None
    user_agent = request.META.get("HTTP_USER_AGENT")
    if is_recording(user_agent):
        return Viewer(ip=client_ip, recording=True)
    return Viewer(
        ip=client_ip,
        user_id=user.id if user is not None else None,
        # None for media servers, so their viewers stay anonymous (see app_name)
        app=app_name(user_agent, client_ip),
        server_device=_server_device(user_agent, client_ip, redis_client),
    )


def _server_device(user_agent, client_ip, redis_client):
    """The device a media server is asking for, when there is only one it could be."""
    if not redis_client or not is_media_server(user_agent, client_ip):
        return None
    try:
        from . import media_servers

        # Four ways to place the request, cheapest first. A media server asks for the stream
        # before it registers what it is playing, so at the instant the request arrives it
        # can often say nothing, and none of these is reliable on its own.
        device = media_servers.sole_device(redis_client)
        if device:
            return f"server|{device}"

        # Several are watching: the one whose channel has just stopped is the one switching
        switching = media_servers.switching_device(redis_client)
        if switching:
            return switching

        # Nobody is watching anything yet, which is what the start of a channel looks like.
        # Wait a moment for the server to catch up rather than serving a viewer we cannot
        # tell apart, which would leave the channel they just left running.
        device = media_servers.wait_for_device(redis_client)
        if device:
            return f"server|{device}"

        # It never answered. Whoever was watching a moment ago is the only one this can be,
        # and is better than nobody: worst case the overlap is applied to the wrong player
        # on the same server, which is where it would have gone anyway.
        device = media_servers.device_a_moment_ago(redis_client)
        return f"server|{device}" if device else None
    except Exception as e:
        logger.debug(f"Could not ask the media server who is watching: {e}")
        return None


def settle_media_server_start(redis_client, channel_uuid, device, previous_channel=None):
    """
    Do the overlap's work for a media server viewer once its server has named them.

    A media server asks for the stream before it registers what it is playing, so at the
    moment of the request it often cannot say which of its players this is. Everything tried
    then is a guess against the clock. This is not: it runs a second or two later, from
    media_servers.bind_device, when the server has said who it is, and does what the request
    could not -- stops the channel this viewer just left, so its slot goes back.

    Doing it late costs nothing that matters. The slot is freed a moment after the new
    channel starts instead of a moment before, and the viewer was never going to be watching
    both. Never raises: this runs in the background, behind a stream that is already playing.
    """
    if not redis_client or not device or not channel_uuid:
        return []
    try:
        if not in_use():
            return []
        viewer = Viewer("", server_device=device)
        stopped = stop_skipped_channels(redis_client, viewer, channel_uuid)
        if stopped:
            logger.info(
                f"Media server: stopped {len(stopped)} channel(s) {device} had left, "
                f"once its server said who was asking"
            )
            record_event(
                redis_client,
                viewer,
                "switched",
                from_channel=str(previous_channel or stopped[0]),
                channel=str(channel_uuid),
                result="the channel it left was stopped once its server said who was asking",
            )
        return stopped
    except Exception as e:
        logger.debug(f"Could not settle the start of channel {channel_uuid}: {e}")
        return []


def _channel_viewers_key(channel_uuid) -> str:
    return CHANNEL_VIEWERS_KEY.format(channel_uuid=channel_uuid)


def record_client_viewer(redis_client, channel_uuid, client_id, viewer):
    """
    Remember which viewer a newly registered client is, while the overlap is in use.

    ClientManager stores ip_address, user_id and user_agent per client, which is everything an
    identity needs. The viewer is also added to the channel's viewer set, which outlives the
    client (see CHANNEL_VIEWERS_KEY).
    """
    if not redis_client or not is_viewer_request(viewer) or not in_use():
        return
    channel_uuid = str(channel_uuid)
    try:
        viewers_key = _channel_viewers_key(channel_uuid)
        redis_client.sadd(viewers_key, _viewer_member(viewer))
        redis_client.expire(viewers_key, CHANNEL_VIEWERS_TTL)
        if viewer.server_device:
            # Kept with the client, so the channel's clients are the same viewer as the
            # request that started it (ClientManager writes the rest of this hash)
            redis_client.hset(
                RedisKeys.client_metadata(channel_uuid, str(client_id)),
                "server_device",
                viewer.server_device,
            )
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
        recording=is_recording(client.get("user_agent")),
        app=app_name(client.get("user_agent"), client.get("ip_address")),
        # Written when the viewer was recognised (see record_client_viewer)
        server_device=client.get("server_device") or None,
    )


def _lan_device_key(viewer, m3u_account):
    """
    LAN Device Tracking: a key for a device on the account's own network, by its IP address,
    user and app, or None when it does not apply (setting off, IP outside the subnets, media
    server or recording, or no app in the User-Agent).

    On a local network every device has its own IP address, so that address plus the player
    app identifies a device without a login and without anything in the stream links. A device
    outside those subnets needs its own Dispatcharr login.
    """
    if (
        m3u_account is None
        or not is_viewer_request(viewer)
        or not viewer.app
        or not account_tracks_lan_devices(m3u_account)
    ):
        return None
    try:
        address = ipaddress.ip_address(viewer.ip)
    except (TypeError, ValueError):
        return None
    if getattr(address, "ipv4_mapped", None):
        address = address.ipv4_mapped
    if not any(address in network for network in account_lan_subnets(m3u_account)):
        return None
    return f"lan|{viewer.ip}|{viewer.user_id or 0}|{viewer.app}"


def identity_key(viewer, m3u_account=None) -> str:
    """
    How a viewer is told apart on an account: IP + user, or IP + user + app when the account
    tracks LAN devices and the viewer is on one of its subnets, or the device a media server
    named for us.
    """
    if viewer.recording:
        return f"recording|{viewer.ip}"
    if viewer.server_device:
        return viewer.server_device
    return _lan_device_key(viewer, m3u_account) or _viewer_key(viewer)


def is_identified(viewer, m3u_account=None) -> bool:
    """
    Whether this request stands for one device on the account: a Dispatcharr user (an Xtream
    login), or a player on one of the account's LAN subnets (its IP address is its own there).

    Everything else is anonymous: media servers, and devices outside the subnets without a
    login, which cannot be told apart from other devices behind the same address.
    """
    return is_viewer_request(viewer) and (
        viewer.user_id is not None
        or viewer.server_device is not None
        or _lan_device_key(viewer, m3u_account) is not None
    )


def may_be_identified(viewer) -> bool:
    """Cheap pre-check without an account: could this viewer be identified anywhere?"""
    return is_viewer_request(viewer) and (
        viewer.user_id is not None
        or viewer.server_device is not None
        or (bool(viewer.app) and lan_tracking_in_use())
    )


def why_not_identified(viewer) -> str:
    """
    Why this viewer could not be told apart, in the words of whatever was missing.

    "Not a viewer Dispatcharr can tell apart" is true and useless: the three ways to be
    identified fail for different reasons and need different things done about them. This
    costs a switch its overlap and, where an account stops the channel being left behind,
    costs a slot, so it is worth saying which one it was.
    """
    try:
        if is_media_server(viewer.app, viewer.ip):
            return (
                "its media server did not say which of its players this is, which happens "
                "while more than one of them is streaming"
            )
        if viewer.user_id is not None:
            return "signed in, but not to an account with the overlap switched on"
        if not viewer.app:
            return "nothing in the request says which player it is"
        if not lan_tracking_in_use():
            return (
                "no login, and no account has LAN subnets set, so players on the network "
                "cannot be told apart by address"
            )
        return "no login, and its address is not in any account's LAN subnets"
    except Exception:
        return "not a viewer Dispatcharr can tell apart"


def _is_viewer(viewer, client, m3u_account=None) -> bool:
    client_viewer = _client_viewer(client)
    if client_viewer.recording or viewer.recording:
        return False
    return identity_key(client_viewer, m3u_account) == identity_key(viewer, m3u_account)


def _accounts_for_profiles(profile_ids):
    """M3U account per profile ID, only loaded while any account tracks LAN devices."""
    profile_ids = {profile_id for profile_id in profile_ids if profile_id is not None}
    if not profile_ids or not lan_tracking_in_use():
        return {}
    from apps.m3u.models import M3UAccountProfile

    return {
        profile.id: profile.m3u_account
        for profile in M3UAccountProfile.objects.select_related("m3u_account").filter(
            id__in=profile_ids
        )
    }


def _viewer_member(viewer) -> str:
    """A viewer as stored in CHANNEL_VIEWERS_KEY: IP, user, app and media server device."""
    return f"{viewer.ip}|{viewer.user_id or 0}|{viewer.app or ''}|{viewer.server_device or ''}"


def _viewer_from_member(member):
    parts = _as_str(member).split("|")
    if len(parts) < 2:
        return None
    ip, user_id = parts[:2]
    app = parts[2] if len(parts) > 2 else ""
    # "server|<device>" is itself split by the separator, so it is put back together
    server_device = "|".join(parts[3:]) if len(parts) > 3 else ""
    return Viewer(
        ip,
        _client_user_id(user_id),
        app=app or None,
        server_device=server_device or None,
    )


# Three questions that sound the same and are not, so each has its own helper:
#   _being_stopped        -- is this channel on its way out for any reason? (do not count its
#                            slot as one this viewer is watching on)
#   _dispatcharr_is_stopping -- is Dispatcharr already stopping it? (do not stop it again, or a
#                            second stop leaves a marker behind that blocks it for a minute)
#   _stop_was_requested   -- was it stopped on purpose, from the dashboard or by a refresh?
#                            (then it is not a channel switch, so its slot is not held)
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


def _only_viewer_was(redis_client, channel_uuid, viewer, m3u_account=None) -> bool:
    members = [
        _viewer_from_member(member)
        for member in redis_client.smembers(_channel_viewers_key(channel_uuid)) or ()
    ]
    keys = {identity_key(member, m3u_account) for member in members if member is not None}
    return keys == {identity_key(viewer, m3u_account)}


def watched_channels_by(redis_client, viewer):
    """
    {profile id: channel uuid} of the live channels this viewer is watching, including a
    channel it was the only viewer of that is waiting out the Channel Shutdown Delay.
    """
    watched = {}
    if not is_viewer_request(viewer):
        return watched

    try:
        channels = list(_active_channels(redis_client))
        accounts = _accounts_for_profiles(profile_id for _uuid, profile_id in channels)
        for channel_uuid, profile_id in channels:
            if profile_id is None or profile_id in watched:
                continue
            account = accounts.get(profile_id)
            clients = list(_channel_clients(redis_client, channel_uuid))
            if any(_is_viewer(viewer, client, account) for client in clients) or (
                not clients
                and _waiting_for_shutdown(redis_client, channel_uuid)
                and _only_viewer_was(redis_client, channel_uuid, viewer, account)
            ):
                watched[profile_id] = channel_uuid
    except Exception as e:
        logger.debug(f"Could not resolve profiles watched by {viewer}: {e}")

    return watched


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


def _dispatcharr_is_stopping(redis_client, channel_uuid) -> bool:
    """Whether Dispatcharr itself has started stopping the channel (not this feature)."""
    if redis_client.exists(RedisKeys.channel_stopping(channel_uuid)):
        return True
    state = _as_str(
        redis_client.hget(RedisKeys.channel_metadata(channel_uuid), ChannelMetadataField.STATE)
    )
    return state == ChannelState.STOPPING


def _stop_channel(channel_uuid):
    """
    Background stop of a skipped or idle channel whose slot was already released.

    When the player leaves the channel at the same moment, Dispatcharr stops it too. A second
    stop that arrives while the first one is still closing the provider connection leaves a
    stopping marker behind (see _clear_leftover_stop_marker), so this does not stop a channel
    that is already stopping or already gone.
    """
    from django.db import close_old_connections
    from core.utils import RedisClient
    from .server import ProxyServer
    from .services.channel_service import ChannelService

    try:
        redis_client = RedisClient.get_client()
        stopping_here = channel_uuid in getattr(ProxyServer.get_instance(), "_stopping_channels", ())
        if (
            stopping_here
            or _dispatcharr_is_stopping(redis_client, channel_uuid)
            or not redis_client.exists(RedisKeys.channel_metadata(channel_uuid))
        ):
            logger.info(
                f"Probation: channel {channel_uuid} is already stopping or stopped; "
                f"not stopping it again"
            )
        else:
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


def _clear_leftover_stop_marker(redis_client, channel_uuid) -> bool:
    # Known limit: a stop that has deleted the channel's metadata but is still closing the
    # provider socket looks exactly like a leftover from here, because the worker doing the
    # stopping knows that only in its own memory. Clearing the marker then lets the channel
    # start again while the old connection is still going away, which can briefly cost the
    # provider two connections. The alternative -- never clearing it -- blocks the channel for
    # a minute every time two stops overlap, which is the failure this exists to fix.
    """
    Remove a stopping marker that no stop will ever clear. Returns whether one was removed.

    Dispatcharr's stop deletes every live:channel:<uuid>:* key, including its own stopping
    marker, and only then closes the provider connection. A second stop command that arrives
    in that moment (for example a skipped channel stopped by this feature while the player
    also disconnects) sets the marker again and returns without cleanup, because a stop is
    already running. The channel then answers "Channel is stopping" (503) for 60 seconds. A
    marker without channel metadata is such a leftover: a stop that is still running has not
    deleted the metadata yet.
    """
    stop_key = RedisKeys.channel_stopping(channel_uuid)
    if not redis_client.exists(stop_key) or redis_client.exists(
        RedisKeys.channel_metadata(channel_uuid)
    ):
        return False
    redis_client.delete(stop_key)
    logger.info(
        f"Probation: removed a leftover stopping marker from channel {channel_uuid}, "
        f"which is no longer running"
    )
    return True


def wait_for_stop_to_finish(redis_client, channel_uuid) -> bool:
    """
    Called by stream_ts before it looks at the requested channel, while the overlap is in use.

    - A leftover stopping marker (see _clear_leftover_stop_marker) is removed, so the channel
      can start instead of answering 503 for up to a minute.
    - When the channel is being stopped by this feature (a skipped or idle channel), wait
      until the stop is done, so the request starts the channel again. Without this, a player
      returning to a channel it just surfed past gets "Channel is stopping" (a 503 with a JSON
      body, which players such as TiviMate show as an error), or joins the channel just
      before the stop ends it. Gives up after STOP_WAIT_SECONDS.

    Other stops that are really running (dashboard, deletes) keep Dispatcharr's normal answer.
    Returns whether it waited or removed a marker.
    """
    channel_uuid = str(channel_uuid)
    try:
        if not redis_client or not in_use():
            return False
        cleared = _clear_leftover_stop_marker(redis_client, channel_uuid)
        if not redis_client.exists(_skipped_stopping_key(channel_uuid)):
            return cleared

        from .services.channel_service import ChannelService

        def stopping():
            if redis_client.exists(_skipped_stopping_key(channel_uuid)):
                return True
            # A second stop may have left its marker behind while we waited
            _clear_leftover_stop_marker(redis_client, channel_uuid)
            return ChannelService.is_channel_teardown_active(channel_uuid)

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


def refusal_retry_delay(stream_id, failures, refused) -> float:
    """
    Seconds the stream manager waits before retrying a connection the provider refused, or 0
    to keep Dispatcharr's normal retry timing.

    Only applies when the connection was refused (closed before any data arrived) and the
    stream's account has the overlap enabled. Retrying a refusing provider within a quarter
    second, three times per channel while surfing, piles up connection attempts and can keep
    the provider refusing.
    """
    if not refused or not stream_id:
        return 0
    try:
        if not in_use():
            return 0

        from core.utils import RedisClient
        from apps.channels.models import Stream
        from apps.m3u.models import M3UAccountProfile

        profile_id = _as_str(RedisClient.get_client().get(f"stream_profile:{stream_id}"))
        profile = (
            M3UAccountProfile.objects.select_related("m3u_account").filter(id=profile_id).first()
            if profile_id
            else None
        )
        if profile is not None:
            account = profile.m3u_account
        else:
            stream = Stream.objects.select_related("m3u_account").filter(id=stream_id).first()
            account = stream.m3u_account if stream else None
        if account is None or not account_allows_probation(account):
            return 0

        delay = REFUSAL_RETRY_SECONDS * failures
        record_event(
            RedisClient.get_client(),
            None,
            "provider refused",
            account=account.name,
            result=f"waiting {delay:.1f}s before attempt {failures + 1}",
        )
        logger.info(
            f"Probation: the provider closed the connection for stream {stream_id} before "
            f"sending any data (probably refused: account full or blocked); waiting "
            f"{delay:.1f}s before trying again (attempt {failures})"
        )
        return delay
    except Exception as e:
        logger.debug(f"Could not check the retry delay for stream {stream_id}: {e}")
        return 0


def _surf_delay_for_channel(channel_uuid, viewer):
    """
    (delay, window) in seconds for a channel: the longest Surfing Delay and Overlap Window of
    its active accounts that have the overlap enabled and recognise this viewer, or (0, 0).
    """
    from django.core.exceptions import ValidationError
    from apps.channels.models import Channel

    try:
        channel = Channel.objects.filter(uuid=channel_uuid).first()
    except (ValidationError, ValueError):
        # Stream previews use a stream hash instead of a channel UUID
        return 0, 0
    if channel is None:
        return 0, 0
    delay = window = 0
    for stream in channel.streams.select_related("m3u_account"):
        account = stream.m3u_account
        if (
            not account
            or not account.is_active
            or not account_allows_probation(account)
            or not is_identified(viewer, account)
        ):
            continue
        account_delay = account_surf_delay_seconds(account)
        if account_delay > 0:
            delay = max(delay, account_delay)
            window = max(window, account_probation_seconds(account))
    return delay, window


def skipped_while_surfing(proxy_server, viewer, channel_uuid) -> bool:
    """
    Called by stream_ts first: while a viewer surfs, wait a moment before its channel is
    requested from the provider, so a channel it passes in the meantime never is.

    Fast channel surfing otherwise opens a provider connection for every channel on the way,
    for streams the player drops a second later, and providers may start refusing
    connections. The delay (the account's Surfing Delay) only applies when:

    - the viewer is identified (a login, or a device on the account's LAN subnets); anonymous
      viewers such as Plex share one identity between several people;
    - its previous channel request was a different channel, within the Overlap Window. The
      first switch after watching something starts at once;
    - the channel is not running yet; joining a running channel costs the provider nothing.

    Returns True when the viewer requested another channel during the delay; stream_ts then
    does not start this one.
    """
    if not may_be_identified(viewer):
        return False
    redis_client = getattr(proxy_server, "redis_client", None)
    channel_uuid = str(channel_uuid)
    try:
        if not redis_client or not in_use():
            return False

        now = time.time()
        token = secrets.token_hex(6)
        key = LAST_REQUEST_KEY.format(viewer=_player_key(viewer))
        previous = _as_str(redis_client.get(key))
        redis_client.setex(key, MAX_PROBATION_SECONDS, f"{now}|{token}|{channel_uuid}")
        if not previous:
            return False
        previous_time, _previous_token, previous_channel = previous.split("|", 2)
        if previous_channel == channel_uuid:
            # The player reconnecting to the same channel
            return False
        if proxy_server.check_if_channel_exists(channel_uuid):
            return False

        delay, window = _surf_delay_for_channel(channel_uuid, viewer)
        since_previous = now - float(previous_time)
        if delay <= 0 or since_previous > window:
            return False

        logger.info(
            f"Probation: {viewer} switched again {since_previous:.1f}s after its previous "
            f"channel; waiting {delay:.1f}s before requesting channel {channel_uuid} from the "
            f"provider"
        )
        gevent.sleep(delay)
        if f"|{token}|" not in (_as_str(redis_client.get(key)) or ""):
            logger.info(
                f"Probation: {viewer} moved on before channel {channel_uuid} started; not "
                f"requesting it from the provider"
            )
            record_event(
                redis_client,
                viewer,
                "skipped while surfing",
                from_channel=previous_channel,
                channel=channel_uuid,
                result=f"moved on during the {delay:.1f}s delay",
            )
            return True
        return False
    except Exception as e:
        logger.debug(f"Could not apply the surfing delay for {viewer}: {e}")
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
    if not may_be_identified(viewer) or not redis_client:
        return []

    try:
        if not in_use() or not any_account_stops_skipped_channels():
            return []

        from apps.m3u.models import M3UAccountProfile

        now = now if now is not None else time.time()
        requested_channel_uuid = str(requested_channel_uuid)

        # (channel uuid, profile id, when this viewer first joined it)
        candidates = []
        channels = list(_active_channels(redis_client))
        accounts = _accounts_for_profiles(profile_id for _uuid, profile_id in channels)
        for channel_uuid, profile_id in channels:
            if (
                profile_id is None
                or channel_uuid == requested_channel_uuid
                or redis_client.exists(_skipped_stopping_key(channel_uuid))
                # Already on its way out; a second stop would leave a stopping marker behind
                or _dispatcharr_is_stopping(redis_client, channel_uuid)
            ):
                continue

            joined = []
            for client in _channel_clients(redis_client, channel_uuid):
                try:
                    joined_at = float(client.get("connected_at"))
                except (TypeError, ValueError):
                    joined_at = None
                if joined_at is None or not _is_viewer(viewer, client, accounts.get(profile_id)):
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
                account_allows_probation(account)
                and account_stops_skipped_channels(account)
                and is_identified(viewer, account)
            ):
                continue
            watched_for = now - joined_at
            if watched_for > account_probation_seconds(account):
                continue
            logger.info(
                f"Probation: stopping skipped channel {channel_uuid} (watched {watched_for:.1f}s) "
                f"for {viewer}, who requested channel {requested_channel_uuid}"
            )
            record_event(
                redis_client,
                viewer,
                "stopped skipped channel",
                channel=channel_uuid,
                account=account.name,
                result=f"watched {watched_for:.1f}s",
            )
            _stop_channel_now(redis_client, channel_uuid, hold_slots)
            stopped.append(channel_uuid)
        return stopped
    except Exception as e:
        logger.error(f"Probation: error stopping skipped channels for {viewer}: {e}", exc_info=True)
        return []


# ── Holding a released slot for its viewer ───────────────────────────────────


def _viewer_key(viewer) -> str:
    """
    A viewer without an account in hand: its address and login only.

    identity_key() is the finer one -- it adds the player app on an account's LAN subnets, and
    the device a media server named -- but it needs the account to know which subnets count.
    This is used where no account is known yet (holds fallback, the last account a viewer was
    on), and is deliberately coarser: two players behind one address are one viewer here.
    Where that difference matters, use _player_key().
    """
    return f"{viewer.ip}|{viewer.user_id or 0}"


def _player_key(viewer) -> str:
    """
    One player, as far as a request can tell, without needing an account.

    The surfing delay and the previous channel are about one player's own behaviour, so they
    must not be shared: two apps on one television, or one login on two devices, would
    otherwise take each other's turn and a legitimate request would be refused as surfing.
    """
    return f"{_viewer_key(viewer)}|{viewer.app or ''}|{viewer.server_device or ''}"


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
        from apps.m3u.connection_pool import profile_credential_release_key
        from apps.m3u.models import M3UAccountProfile

        profile = M3UAccountProfile.objects.select_related("m3u_account").filter(id=profile_id).first()
        if profile is None or profile.max_streams == 0:
            return
        account = profile.m3u_account
        if not account_allows_probation(account):
            return

        # Distinct viewers as the account tells them apart (see identity_key)
        viewers = {}
        for client in _channel_clients(redis_client, channel_uuid):
            if client.get("ip_address"):
                client_viewer = _client_viewer(client)
                viewers.setdefault(identity_key(client_viewer, account), client_viewer)
        if len(viewers) != 1:
            return
        ((viewer_key, viewer),) = viewers.items()
        if viewer.recording:
            return
        if not is_identified(viewer, account):
            return

        seconds = account_probation_seconds(account)
        expires_at = time.time() + seconds
        # Server Groups: the shared login counter this slot used, still known before release
        credential_key = _as_str(redis_client.get(profile_credential_release_key(profile.id)))
        key = _held_slots_key(profile.id)
        redis_client.hset(key, viewer_key, f"{expires_at}|{credential_key or ''}")
        # Expired entries are ignored anyway; the TTL only cleans up the hash
        redis_client.expire(key, MAX_PROBATION_SECONDS + 5)
        if credential_key:
            login_key = _held_login_slots_key(credential_key)
            redis_client.hset(login_key, viewer_key, f"{expires_at}|{profile.id}")
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
        holds = redis_client.hgetall(key) or {}
        if not holds:
            return 0
        own_key = identity_key(viewer, profile.m3u_account) if viewer is not None else None
        now = time.time()
        held, expired = 0, []
        for viewer_key, value in holds.items():
            viewer_key = _as_str(viewer_key)
            if _parse_hold(value)[0] <= now:
                expired.append(viewer_key)
            elif viewer_key != own_key:
                held += 1
        if expired:
            redis_client.hdel(key, *expired)
        # Holds end with the window; one turned off meanwhile does not wait for that. This
        # applies to a Server Group's shared login too: the hold was made by this account, so
        # switching the account off releases it on both counters.
        if held and not account_allows_probation(profile.m3u_account):
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


def take_held_slot(redis_client, profile, viewer, channel_uuid=None):
    """Use up this viewer's hold after it got a slot on the profile (or its shared login) again."""
    if not is_viewer_request(viewer) or profile.max_streams == 0:
        return
    try:
        if not in_use():
            return
        from apps.m3u.connection_pool import profile_credential_release_key

        viewer_key = identity_key(viewer, profile.m3u_account)
        taken = _drop_hold(redis_client, viewer_key, profile.id)
        credential_key = _as_str(redis_client.get(profile_credential_release_key(profile.id)))
        if credential_key:
            taken = _drop_login_hold(redis_client, viewer_key, credential_key) or taken
        if taken:
            logger.info(f"Probation: {viewer} took its held slot on profile {profile.id}")
            record_event(
                redis_client,
                viewer,
                "held slot",
                channel=channel_uuid,
                account=profile.m3u_account.name,
                result="playing",
            )
    except Exception as e:
        logger.debug(f"Could not clear held slot for {viewer}: {e}")


# ── Account preference when switching ────────────────────────────────────────


def _last_profile_key(viewer_key) -> str:
    return LAST_PROFILE_KEY.format(viewer=viewer_key)


def _last_profile_id(redis_client, viewer):
    """The profile this viewer was last assigned, also when only its LAN address matched."""
    last_profile_id = _as_str(redis_client.get(_last_profile_key(_viewer_key(viewer))))
    if not last_profile_id and viewer.app:
        # Stored by remember_viewer_profile() when the account tracked this LAN device
        last_profile_id = _as_str(
            redis_client.get(
                _last_profile_key(f"lan|{viewer.ip}|{viewer.user_id or 0}|{viewer.app}")
            )
        )
    return int(last_profile_id) if last_profile_id else None


def remember_viewer_profile(redis_client, viewer, profile, m3u_account):
    """Remember the profile a viewer was just assigned, on accounts with an account preference."""
    if not is_viewer_request(viewer) or not redis_client:
        return
    if not account_allows_probation(m3u_account):
        return
    if account_switch_preference(m3u_account) == ACCOUNT_PREFERENCE_ORDER:
        return
    try:
        redis_client.setex(_last_profile_key(_viewer_key(viewer)), LAST_PROFILE_TTL, profile.id)
        lan_key = _lan_device_key(viewer, m3u_account)
        if lan_key:
            redis_client.setex(_last_profile_key(lan_key), LAST_PROFILE_TTL, profile.id)
    except Exception as e:
        logger.debug(f"Could not remember profile for {viewer}: {e}")


@_never_breaks_a_stream("keep a viewer on the account it is on")
def reserve_sticky_slot(channel, redis_client, viewer):
    """
    Account preference "same": reserve a slot on the M3U profile this viewer is watching on
    or was just assigned, ahead of the channel's normal stream order.

    Only streams on accounts with probation_enabled and preference "same" are considered
    (a viewer that cannot be told apart never gets one). A free slot on the
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
        and is_identified(viewer, account),
    )
    if not candidates or channel.get_stream_profile().is_redirect():
        return None

    watched = watched_channels_by(redis_client, viewer)
    watched_profile_ids = list(watched)
    preferred_profile_ids = list(watched_profile_ids)
    last_profile_id = _last_profile_id(redis_client, viewer)
    if last_profile_id and last_profile_id not in preferred_profile_ids:
        preferred_profile_ids.append(last_profile_id)

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
            take_held_slot(redis_client, profile, viewer, channel.uuid)

            redis_client.set(f"channel_stream:{channel.id}", stream.id)
            redis_client.set(f"stream_profile:{stream.id}", profile.id)
            window = ""
            if on_overlap:
                seconds = account_probation_seconds(stream.m3u_account)
                mark_probation(redis_client, channel, stream.id, profile.id, seconds)
                window = f" on overlap slot, window {seconds}s"
            remember_viewer_profile(redis_client, viewer, profile, stream.m3u_account)
            record_event(
                redis_client,
                viewer,
                "overlap slot" if on_overlap else "same account",
                channel=channel.uuid,
                from_channel=watched.get(profile.id),
                account=stream.m3u_account.name,
                result="waiting" if on_overlap else "playing",
            )
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


def _release_viewer_holds(redis_client, viewer, profile_ids, accounts):
    """
    Give up the slots held for this viewer on the profiles it is leaving.

    "Use another account" moves a viewer to a different account, so the slots kept for it on
    the old one would be held for a viewer that is never coming back: nobody else could use
    them until the window ran out. The accounts are the ones already loaded for those
    profiles, because a hold is keyed by identity and identity depends on the account.
    """
    for profile_id in profile_ids:
        _drop_hold(redis_client, identity_key(viewer, accounts.get(profile_id)), profile_id)


@_never_breaks_a_stream("move a viewer to another account")
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

    left_channels = watched_channels_by(redis_client, viewer)
    left_profile_ids = set(left_channels)
    last_profile_id = _last_profile_id(redis_client, viewer)
    if last_profile_id:
        left_profile_ids.add(last_profile_id)
    left_profile_ids.update(
        profile_id
        for profile_id, account in accounts.items()
        if redis_client.hget(_held_slots_key(profile_id), identity_key(viewer, account))
    )
    left_profile_ids = {
        profile_id
        for profile_id in left_profile_ids
        if profile_id in accounts
        and account_allows_probation(accounts[profile_id])
        and account_alternates_viewers(accounts[profile_id])
        and is_identified(viewer, accounts[profile_id])
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
        take_held_slot(redis_client, profile, viewer, channel.uuid)
        # The viewer does not need the slot it left any more; let others have it
        _release_viewer_holds(redis_client, viewer, left_profile_ids, accounts)
        remember_viewer_profile(redis_client, viewer, profile, account)
        record_event(
            redis_client,
            viewer,
            "another account",
            channel=channel.uuid,
            from_channel=left_channels.get(next(iter(left_profile_ids), None)),
            account=account.name,
            result="playing",
        )
        logger.info(
            f"Probation: moving {viewer} to profile {profile.id} ({profile.name}) for channel "
            f"{channel.uuid}, stream {stream.id} ({current_count}/{profile.max_streams}), "
            f"leaving profile(s) {sorted(left_profile_ids)} (use another account)"
        )
        return stream.id, profile.id, None, True
    return None


# ── Overlap: reserve, record, resolve, monitor ───────────────────────────────


@_never_breaks_a_stream("give a viewer the overlap slot")
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
    unknown = [
        (stream, profile)
        for stream, profile in candidates
        if not is_identified(viewer, stream.m3u_account)
    ]
    if unknown:
        # A viewer that cannot be told apart takes no part: an extra connection given to the
        # wrong one is a stream stopped for somebody else.
        candidates = [pair for pair in candidates if pair not in unknown]
        if not candidates:
            log_not_used(
                channel.uuid,
                viewer,
                f"{viewer} has no login, is not on a LAN subnet of these accounts, and no "
                f"media server said which of its devices is asking",
            )
    if not candidates:
        return None
    if channel.get_stream_profile().is_redirect():
        # Redirects hand the provider URL to the player and release the slot, so there is
        # no proxied stream whose overlap could be resolved.
        log_not_used(channel.uuid, viewer, "redirect stream profile")
        return None

    watched = watched_channels_by(redis_client, viewer)
    watched_candidates = [
        (stream, profile) for stream, profile in candidates if profile.id in watched
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
        event_id = record_event(
            redis_client,
            viewer,
            "overlap slot",
            channel=channel.uuid,
            from_channel=watched.get(profile.id),
            account=stream.m3u_account.name,
            result="waiting",
        )
        mark_probation(redis_client, channel, stream.id, profile.id, seconds, event_id)
        take_held_slot(redis_client, profile, viewer, channel.uuid)
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


def mark_probation(redis_client, channel, stream_id, profile_id, seconds, event_id=None):
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
            "event_id": event_id or "",
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
    """
    Forget a probation: the record, its place in the pending set, and the monitor's lease.

    The lease goes with the record on purpose. It is what stops two workers monitoring the
    same channel, so leaving it behind would keep another worker from taking over a probation
    that came back, and leaving the record without the lease would let two of them resolve it.
    """
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
        seconds_taken = now - float(record.get("started_at", now))
        logger.info(
            f"Probation: channel {channel_uuid} confirmed on profile {profile_id} "
            f"after {seconds_taken:.1f}s"
        )
        update_event(
            redis_client, record.get("event_id"), result=f"confirmed after {seconds_taken:.1f}s"
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
        update_event(redis_client, record.get("event_id"), result="moved to another account")
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
    update_event(
        redis_client, record.get("event_id"), result="stopped: no slot free within the window"
    )
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


def _assignment_channel_ids(owner_id, stream_id):
    """
    Live proxy IDs an assignment can belong to: a channel's UUID for channel_stream:<channel id>,
    and the stream hash for a stream preview (channel_stream:<stream id> -> <stream id>).
    """
    from apps.channels.models import Channel, Stream

    channel_ids = []
    channel_uuid = Channel.objects.filter(id=owner_id).values_list("uuid", flat=True).first()
    if channel_uuid:
        channel_ids.append(str(channel_uuid))
    if owner_id == stream_id:
        stream_hash = Stream.objects.filter(id=stream_id).values_list("stream_hash", flat=True).first()
        if stream_hash:
            channel_ids.append(stream_hash)
    return channel_ids


def _live_channel_ids(redis_client):
    """
    IDs with at least one live:channel:<id>:* key that expires: metadata, buffer chunks,
    clients, owner, stop and disconnect markers. Those only exist while the channel runs, or
    shortly after. Keys without an expiry (such as the buffer index) outlive an unclean stop,
    so they do not count.
    """
    keys = [_as_str(key) for key in redis_client.scan_iter(match="live:channel:*", count=1000)]
    live_ids = set()
    for start in range(0, len(keys), 500):
        batch = keys[start:start + 500]
        pipe = redis_client.pipeline(transaction=False)
        for key in batch:
            pipe.ttl(key)
        for key, ttl in zip(batch, pipe.execute()):
            parts = key.split(":", 3)
            if len(parts) >= 3 and ttl is not None and int(ttl) >= 0:
                live_ids.add(parts[2])
    return live_ids


def release_abandoned_slots(redis_client, now=None) -> list:
    """
    Release slots whose channel no longer exists in the live proxy. Returns the released
    assignment keys.

    A channel holds its slot through channel_stream:<channel id>, stream_profile:<stream id>
    and the profile's counter, none of which expire, while all of its live:channel:<uuid>:*
    keys do (metadata, buffer chunks, clients, owner). When Dispatcharr stops without cleaning
    up (a reboot, a service restart, a crash) those keys expire but the slot stays counted,
    and the account looks full until someone fixes Redis by hand.

    A channel that is starting, streaming, waiting out a shutdown delay or being stopped always
    has some of those expiring keys (a streaming channel writes buffer chunks every fraction of
    a second), so an assignment is only released when its channel has had none at all on every
    check for ABANDONED_SLOT_GRACE seconds.

    Called from every worker's proxy cleanup loop; a Redis lock makes one worker do the work
    every SWEEP_INTERVAL seconds.
    """
    if not redis_client:
        return []
    try:
        if not redis_client.set(SWEEP_LOCK_KEY, "1", nx=True, ex=SWEEP_INTERVAL):
            return []

        from apps.m3u.connection_pool import release_profile_slot

        now = now if now is not None else time.time()
        first_seen = {
            _as_str(key): _parse_hold(value)[0]
            for key, value in (redis_client.hgetall(ABANDONED_SLOTS_KEY) or {}).items()
        }

        assignment_keys = [
            _as_str(key) for key in redis_client.scan_iter(match="channel_stream:*", count=500)
        ]
        if not assignment_keys and not first_seen:
            return []
        live_ids = _live_channel_ids(redis_client)

        # (assignment key, owner id, stream id, running)
        assignments = []
        for key in assignment_keys:
            key = _as_str(key)
            try:
                owner_id = int(key.split(":", 1)[1])
                stream_id = int(_as_str(redis_client.get(key)))
            except (TypeError, ValueError):
                continue
            running = any(
                channel_id in live_ids
                for channel_id in _assignment_channel_ids(owner_id, stream_id)
            )
            assignments.append((key, owner_id, stream_id, running))

        streams_in_use = {stream_id for _k, _o, stream_id, running in assignments if running}
        not_running = set()
        released = []
        for key, owner_id, stream_id, running in assignments:
            if running:
                continue
            not_running.add(key)
            if key not in first_seen:
                redis_client.hset(ABANDONED_SLOTS_KEY, key, str(now))
                continue
            if now - first_seen[key] < ABANDONED_SLOT_GRACE:
                continue

            redis_client.delete(key)
            not_running.discard(key)
            profile_id = _as_str(redis_client.get(f"stream_profile:{stream_id}"))
            if stream_id in streams_in_use or not profile_id:
                # Another running channel shares the stream's profile record, or no slot was
                # counted (stream previews keep an empty assignment after they end)
                logger.info(f"Probation: removed a leftover assignment {key} (stream {stream_id})")
                continue
            redis_client.delete(f"stream_profile:{stream_id}")
            release_profile_slot(int(profile_id), redis_client)
            logger.warning(
                f"Probation: released the slot on profile {profile_id} that {key} (stream "
                f"{stream_id}) still held; its channel has not been running for "
                f"{now - first_seen[key]:.0f}s (for example after a restart or crash)"
            )
            released.append(key)

        finished = [key for key in first_seen if key not in not_running]
        if finished:
            redis_client.hdel(ABANDONED_SLOTS_KEY, *finished)
        redis_client.expire(ABANDONED_SLOTS_KEY, ABANDONED_SLOT_GRACE * 10)
        return released
    except Exception as e:
        logger.debug(f"Could not check for abandoned slots: {e}")
        return []

