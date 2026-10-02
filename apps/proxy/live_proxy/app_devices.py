"""What a player app says about itself, for the apps that are built to say it (arrTV).

Everything else Dispatcharr knows about who is watching is guessed: the address a request
comes from, the login it used, the User-Agent. On the real installation that guess failed in
the plainest way -- a TV (arrTV on a SHIELD) and a Mac (Chrome) on the same admin login both
reached the server through the VPN as 192.168.65.3, so to Force Close they were one device,
and each one's channel start closed the other's stream. An app that knows which device it is
can simply say so, and then nothing needs guessing.

Two switches, both off, so that off is stock (a header Dispatcharr is not asked to read
changes nothing):

- **devices**: `X-Dispatch-Device` (and `X-Dispatch-Device-Name`) identify the device, with the
  login it uses, in place of address and app. `X-Dispatch-Multiview` marks a request as a
  tile of a multiview session: it closes nothing of its device, since the device means to
  watch several channels at once.
- **switch_hints**: `X-Dispatch-Previous-Channel` says which channel the device is leaving,
  and that channel is closed the moment the new one is asked for -- its slot is free for the
  new channel, instead of the old one lingering until the player's connection times out or
  Force Close works out that it was left.

- **stall_switch** (needs devices): arrTV says when its picture stutters, and the channel moves
  to its next stream at once (app_stalls).
- **own_stream**: an arrTV device that cannot use the stream a channel is playing gets another
  stream of that channel to itself (app_own_streams). What it cannot use includes what it says
  it cannot decode (`X-Dispatch-Max-Video`, read with devices on).

Every header also works as a query parameter (`dm_device`, `dm_device_name`, `dm_multiview`,
`dm_previous`), for what plays a link without letting the app set headers (a Cast receiver).

The hand-over for the app's developer is fork/arrTV-integration.md.
"""

import logging
import re
import time

logger = logging.getLogger("live_proxy")

SETTINGS_KEY = "app-integration"
# reports: an app may send error reports (see app_reports)
# home_networks / outside_max_quality: an arrTV device outside the home networks is given
# a stream no better than this ("FHD", "HD", "SD"; "" is no limit, 4K included, since nothing
# is better than 4K) -- see ordered_for
# stall_switch: a channel arrTV stutters on moves to its next stream (see app_stalls)
# own_stream: arrTV gets a stream of its own when a channel plays one it cannot use
# (see app_own_streams)
# fast_failover: a channel an arrTV device starts moves on from a stream that connects and
# sends nothing after FAST_START_GRACE seconds and one health check, not stock's start grace
# (60 s) and three (see fast_start_grace)
# alternatives: tell arrTV with each stream how many other streams the channel could switch to
# for it right now (app_alternatives, X-Dispatch-Alternatives), so it waits less where there
# are several; fast_grace / fast_grace_many: faster failover's wait in seconds with one usable
# alternative, and with two or more (a channel with none keeps the stock grace)
# keep_past_days: a guide refresh keeps the finished programmes of this many days (0-7),
# which it otherwise deletes with the rest (see apps.channels.guide_past); 0 is stock
# guide_choice: whoever watches a channel in arrTV may put it on another guide, from the
# guides that have something on now (see app_guides); guide_choice_sources: the EPG source
# ids to offer from, comma separated ("" is every active one that is not a dummy)
DEFAULTS = {
    "devices": False, "switch_hints": False, "reports": False,
    "home_networks": "", "outside_max_quality": "", "stall_switch": False,
    "own_stream": False, "fast_failover": False, "alternatives": False,
    "fast_grace": 5, "fast_grace_many": 3,
    "guide_choice": False, "guide_choice_sources": "",
    "keep_past_days": 0,
    # Server rewind (live_proxy/rewind.py, fork/pause-resume.md §4.3): watched channels kept on
    # the server so a TV can pause and rewind without its own disk. Minutes always kept, the
    # longest pause kept, and the disk budget in GB
    "rewind": True, "rewind_minutes": 60, "rewind_max_pause_minutes": 240, "rewind_budget_gb": 20,
    # Look back gets a provider (fork/lookback-priority.md): another viewer's live channel may be
    # moved to a stream of its own so a look back can have the only provider with the archive;
    # the moved viewer is told
    "look_back_priority": True, "look_back_priority_notify": True,
}
# The seconds settings: whole seconds within these bounds
GRACE_BOUNDS = (1, 60)
# Settings with bounds of their own
BOUNDS = {
    "rewind_minutes": (5, 240),
    "rewind_max_pause_minutes": (15, 1440),
    "rewind_budget_gb": (1, 4000),
}

# Faster failover: which channels an arrTV device started, and how many other streams each
# could switch to, for the start phase only. (v212 wrote "1" under fast_start:, v213 the grace
# under fast_grace:; this key holds the count, so neither is read as one.)
FAST_START_KEY = "live:app_devices:fast_alternatives:{channel}"
FAST_START_TTL = 120
# The default of fast_grace, for the tests and the documentation
FAST_START_GRACE = 5
# From this many usable alternatives on, fast_grace_many applies (two: the user's channels
# have three streams at most, so at most two others)
MANY_ALTERNATIVES = 2
QUALITY_LIMITS = ("FHD", "HD", "SD")

# Read on every stream request, so kept for a few seconds rather than asked of the database
_HELD = {"at": 0.0, "value": None}
HELD_SECONDS = 10

# What devices have said their names are, for the Diagnostics page: {device key: json}
NAMES_KEY = "live:app_devices:names"
NAMES_TTL = 30 * 24 * 3600

# A random identifier the app made once: letters, digits and dashes, as a UUID is written.
# Anything else is ignored rather than trusted, since it ends up in Redis keys and the log.
_DEVICE_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_CHANNEL_ID = re.compile(r"^[A-Za-z0-9-]{1,80}$")

HEADERS = {
    "device": ("HTTP_X_DISPATCH_DEVICE", "dm_device"),
    "device_name": ("HTTP_X_DISPATCH_DEVICE_NAME", "dm_device_name"),
    "multiview": ("HTTP_X_DISPATCH_MULTIVIEW", "dm_multiview"),
    "previous": ("HTTP_X_DISPATCH_PREVIOUS_CHANNEL", "dm_previous"),
    "max_video": ("HTTP_X_DISPATCH_MAX_VIDEO", "dm_max_video"),
}


def _as_kind(key, value):
    """A setting as the kind its default is: the switches on or off, the rest as text."""
    if isinstance(DEFAULTS[key], bool):
        return bool(value)
    if key == "keep_past_days":
        # Days of finished programmes a guide refresh keeps (apps.channels.guide_past)
        from apps.channels.guide_past import MOST_DAYS

        try:
            return max(0, min(MOST_DAYS, int(float(value or 0))))
        except (TypeError, ValueError):
            return 0
    if isinstance(DEFAULTS[key], int):
        low, high = BOUNDS.get(key, GRACE_BOUNDS)
        try:
            return max(low, min(high, int(float(value))))
        except (TypeError, ValueError):
            return DEFAULTS[key]
    if key == "guide_choice_sources":
        # Ids only: given as a list by the settings page, kept as text like the other rows
        parts = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
        return ",".join(str(int(p)) for p in parts if str(p).strip().isdigit())
    text = str(value or "").strip()
    if key == "outside_max_quality":
        return text.upper() if text.upper() in QUALITY_LIMITS else ""
    return text[:2000]


def load_settings():
    now = time.monotonic()
    if _HELD["value"] is not None and now - _HELD["at"] < HELD_SECONDS:
        return dict(_HELD["value"])
    values = dict(DEFAULTS)
    try:
        from core.models import CoreSettings

        stored = CoreSettings.objects.filter(key=SETTINGS_KEY).first()
        if stored and isinstance(stored.value, dict):
            values.update({k: _as_kind(k, stored.value[k]) for k in DEFAULTS if k in stored.value})
    except Exception as e:
        logger.debug(f"App devices: could not read the settings: {e}")
    _HELD.update(at=now, value=dict(values))
    return values


def save_settings(given):
    from core.models import CoreSettings

    before = load_settings()
    values = {**before, **{k: _as_kind(k, given[k]) for k in DEFAULTS if k in (given or {})}}
    CoreSettings.objects.update_or_create(
        key=SETTINGS_KEY, defaults={"name": "App integration", "value": values}
    )
    _HELD.update(at=0.0, value=None)
    # The guide choice's lists need programmes for guides no channel uses: read them when it
    # goes on, and stop keeping them when it goes off, so off is stock again after the next
    # refresh's clean-up
    if values["guide_choice"] != before["guide_choice"]:
        try:
            from . import app_guides

            if values["guide_choice"]:
                app_guides.start_preload()
            else:
                app_guides.forget_kept()
                # A preload still going stops at its next batch; switched on again before
                # then, a new one is not refused because of it
                app_guides.end_preload("stopped")
        except Exception as e:
            logger.warning(f"arrTV guides: could not start or stop keeping guides: {e}")
    return values


def _said(request, what):
    header, param = HEADERS[what]
    value = request.META.get(header)
    if value is None:
        try:
            value = request.GET.get(param)
        except Exception:
            value = None
    return str(value).strip() if value else ""


def declared_device(request):
    """
    The device the app says it is, or "" -- only while devices are switched on.

    The request is looked at before the settings: every stream request asks this, and
    one that carries no device (every player but arrTV) should cost a header lookup and
    nothing more (HANDOVER §7, the hook meant to cost nothing).
    """
    device = _said(request, "device")
    if not device or not load_settings()["devices"]:
        return ""
    return device if _DEVICE_ID.match(device) else ""


def declared_multiview(request):
    """The multiview session a tile says it belongs to, or ""."""
    session = _said(request, "multiview")
    if not session or not load_settings()["devices"]:
        return ""
    return session if _SESSION_ID.match(session) else ""


def mark_fast_start(redis_client, request, channel_id, alternatives=None):
    """
    An arrTV device (declared) starts this channel with "faster failover" on: mark the
    channel for FAST_START_TTL seconds with how many other streams it could switch to
    (app_alternatives.count), so that while its stream has sent nothing the stream manager
    moves on after a few seconds rather than stock's minute (fast_grace_for). IPTV answers
    within a second or two; stock's 60 s start grace is sized for sources that need to lock
    first, and a dead stream cost a viewer over a minute.

    Nowhere to go is never hurried (the user's rule): no other stream it can play, or none
    on a provider with a connection free -- another viewer holding them -- and a stream of
    its own, which has no failover at all (alternatives None). Leaving a stream then could
    only end on the fallback. Other streams of the provider the channel is on count: the
    channel's own connection moves with it. Nothing is written when switched off.
    """
    # arrTV first: a header lookup, before the settings (every stream request passes here)
    if redis_client is None or not alternatives or not declared_device(request):
        return
    if not load_settings().get("fast_failover"):
        return
    try:
        redis_client.set(FAST_START_KEY.format(channel=channel_id), str(int(alternatives)), ex=FAST_START_TTL)
    except Exception as e:
        logger.debug(f"Faster failover: could not mark {channel_id}: {e}")


def fast_start_alternatives(redis_client, channel_id):
    """
    How many other streams the arrTV device that started this channel could switch to, as
    marked by mark_fast_start, or None: not marked, the stock grace applies. One Redis read,
    no settings: the stream manager asks it once per start, for every channel.
    """
    if redis_client is None:
        return None
    try:
        value = redis_client.get(FAST_START_KEY.format(channel=channel_id))
        if value is None:
            return None
        return int(float(value.decode() if isinstance(value, bytes) else value))
    except Exception as e:
        logger.debug(f"Faster failover: could not read {channel_id}: {e}")
        return None


def fast_grace_for(remaining):
    """
    The wait before leaving a stream that has sent nothing, with this many other streams
    still left to go to, or None for stock's grace. Asked again at every step of the walk:
    with two or more left the shorter wait (fast_grace_many), with one left the longer
    (fast_grace) -- a channel with just a couple of streams is not skipped through too fast
    -- and with none left, stock's. The settings are read here, so switching it off or
    changing a wait takes effect at once.
    """
    if remaining is None or remaining <= 0:
        return None
    settings = load_settings()
    if not settings.get("fast_failover"):
        return None
    return settings["fast_grace_many"] if remaining >= MANY_ALTERNATIVES else settings["fast_grace"]


def fast_start_grace(redis_client, channel_id, left_already=0):
    """The wait for this channel's start, having left `left_already` streams, or None."""
    alternatives = fast_start_alternatives(redis_client, channel_id)
    if alternatives is None:
        return None
    return fast_grace_for(alternatives - left_already)


def declared_previous_channel(request):
    """
    The channel the app says it is leaving, as its UUID, or "" -- only while switch hints
    are on. Given as the UUID (the /proxy/ts/stream/<uuid> links) or as the channel's number
    id (the Xtream /live/<user>/<pass>/<id> links, where that id is all the app has).
    """
    channel = _said(request, "previous")
    if not channel or not load_settings()["switch_hints"]:
        return ""
    if not _CHANNEL_ID.match(channel):
        return ""
    if channel.isdigit():
        try:
            from apps.channels.models import Channel

            uuid = Channel.objects.filter(id=int(channel)).values_list("uuid", flat=True).first()
        except Exception:
            uuid = None
        return str(uuid) if uuid else ""
    return channel


def declared_max_quality(request):
    """
    The best picture the device says it can decode, as a quality ("FHD", "HD", "SD"), or ""
    for no limit. Given as the height it can play ("1080") or as the quality itself: a
    Chromecast HD says 1080, and a 4K stream is then nothing it can use.
    """
    said = _said(request, "max_video").upper().rstrip("P")
    if not said or not load_settings()["devices"]:
        return ""
    if said in QUALITY_LIMITS:
        return said
    if not said.isdigit():
        return ""
    height = int(said)
    if height >= 2000:
        return ""
    return "FHD" if height >= 1000 else "HD" if height >= 700 else "SD" if height > 0 else ""


def _lowest(*limits):
    """The strictest of several quality limits, "" when there is none."""
    from apps.channels.channel_manager import QUALITY_LABELS

    given = [limit for limit in limits if limit in QUALITY_LABELS]
    return max(given, key=QUALITY_LABELS.index) if given else ""


def device_key(user_id, device):
    """
    A declared device as the channel switch code keys viewers: with its login, so a device
    can only ever be the same viewer as requests made with its own login.
    """
    return f"app|{user_id or 0}|{device}"


def is_declared(key):
    return bool(key) and str(key).startswith("app|")


def remember_name(redis_client, key, request, username=""):
    """Keep what the device calls itself, for Diagnostics. Never costs a viewer anything."""
    name = _said(request, "device_name")[:80]
    if not redis_client or not key:
        return
    try:
        import json

        redis_client.hset(NAMES_KEY, key, json.dumps({
            "name": name, "user": username or "", "seen": time.time(),
        }))
        redis_client.expire(NAMES_KEY, NAMES_TTL)
    except Exception as e:
        logger.debug(f"App devices: could not keep the name of {key}: {e}")


def device_name(redis_client, key):
    """"Living room SHIELD", or "" for a device that never said."""
    if not redis_client or not is_declared(key):
        return ""
    try:
        import json

        raw = redis_client.hget(NAMES_KEY, key)
        if not raw:
            return ""
        said = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        name = said.get("name") or ""
        return f"{said['user']} · {name}" if name and said.get("user") else name
    except Exception:
        return ""


# arrTV's own User-Agent, for requests from a version that does not declare its device yet:
# "AerioTV/1.4.0-arr. (Android; SHIELD Android TV)" -- the "-arr" is what makes it arrTV
_ARRTV_AGENT = re.compile(r"arrtv|aeriotv[^ ]*-arr", re.IGNORECASE)


def is_arrtv(viewer):
    """Whether this request is arrTV's: a device it declared, or its User-Agent."""
    if viewer is None:
        return False
    return is_declared(getattr(viewer, "server_device", None)) or bool(
        _ARRTV_AGENT.search(getattr(viewer, "app", None) or "")
    )


def _at_home(ip, networks_text):
    import ipaddress

    try:
        from .probation import parse_lan_subnets

        networks = parse_lan_subnets(networks_text)
    except Exception:
        networks = []
    if not networks:
        return None
    try:
        address = ipaddress.ip_address(ip)
        if getattr(address, "ipv4_mapped", None):
            address = address.ipv4_mapped
    except (TypeError, ValueError):
        return None
    return any(address in ipaddress.ip_network(n, strict=False) for n in networks)


def quality_limit_for(viewer):
    """
    The best quality this viewer should be given ("FHD", "HD", "SD"), or "" for none: an arrTV
    device whose address is outside the home networks, when a limit is set. Nothing is
    limited while the home networks are empty -- without them everything would be outside.
    Also the best the device says it can decode (X-Dispatch-Max-Video), and a quality it
    stuttered its way down to where it is now (see app_stalls): the lowest of them.
    """
    # Asked on every channel start: anybody but arrTV costs nothing, not even the settings
    if not is_arrtv(viewer):
        return ""
    settings = load_settings()
    limit = settings.get("outside_max_quality") or ""
    at_home = _at_home(getattr(viewer, "ip", ""), settings.get("home_networks"))
    limit = limit if limit and at_home is False else ""
    # What the device says it can decode at all, wherever it is
    limit = _lowest(limit, getattr(viewer, "max_quality", None) or "")
    # What this device turned out to manage where it is now (app_stalls), when that is less
    if settings.get("stall_switch") and settings.get("devices") and is_declared(
        getattr(viewer, "server_device", None)
    ):
        try:
            from core.utils import RedisClient

            from .app_stalls import held_quality

            held = held_quality(RedisClient.get_client(), viewer)
        except Exception:
            held = ""
        limit = _lowest(limit, held)
    return limit


def ordered_for(viewer, streams):
    """
    A channel's streams in the order they are tried for this viewer. For an arrTV device
    outside the home networks with a limit set, the streams within the limit come first --
    on a VPN or a phone connection an FHD stream stutters where an HD one plays -- then the
    better ones, so a channel that has nothing within the limit still plays; and the custom
    fallback stays last, as always. Everything else: the order as it is.
    """
    streams = list(streams)
    limit = quality_limit_for(viewer)
    if not limit:
        return streams
    from apps.channels.channel_manager import QUALITY_LABELS, quality_of

    most = QUALITY_LABELS.index(limit)

    def within(stream):
        label, rank, _probed = quality_of(stream.name, stream.stream_stats)
        # A stream that says nothing about its picture is not held against it
        return not label or rank >= most

    real = [s for s in streams if not s.is_custom]
    allowed = [s for s in real if within(s)]
    if len(allowed) == len(real):
        return streams
    logger.info(
        f"arrTV outside home ({getattr(viewer, 'ip', '')}): {limit} at most, "
        f"{len(real) - len(allowed)} better stream(s) tried last"
    )
    return allowed + [s for s in real if s not in allowed] + [s for s in streams if s.is_custom]


# The limit of the device that started a channel, for the failover (see failover_order)
CHANNEL_LIMIT_KEY = "live:app_devices:channel_limit:{channel_uuid}"
CHANNEL_LIMIT_TTL = 24 * 3600


def _limits_in_use(settings):
    return bool(settings.get("devices") or settings.get("outside_max_quality"))


def remember_channel_limit(redis_client, channel_uuid, viewer):
    """
    Keep the limit of the arrTV device starting a channel, with when. Called where a channel
    picks its stream, which is when it starts: a channel is one stream for everyone on it,
    and the one who starts it is who it was chosen for.

    Anybody else starting a channel costs nothing here -- no settings, no Redis -- as with
    everything this fork hooks into stock. So a limit is never taken away; failover_order
    believes it only for the run of the channel it was kept for (by the channel's start time).
    Without a viewer it is not somebody starting the channel at all: stock re-reserves a
    running channel's stream that way when a shutdown is cancelled.
    """
    if viewer is None or not is_arrtv(viewer) or not redis_client:
        return
    try:
        limit = quality_limit_for(viewer)
        if limit:
            redis_client.setex(
                CHANNEL_LIMIT_KEY.format(channel_uuid=channel_uuid), CHANNEL_LIMIT_TTL, f"{limit}|{time.time()}"
            )
    except Exception as e:
        logger.debug(f"App devices: could not keep the limit of channel {channel_uuid}: {e}")


# A channel's start time is written a moment after its stream is picked; a limit kept longer
# than this before it was for an earlier run of the channel
LIMIT_BEFORE_START = 120


def _limit_of_this_run(redis_client, channel_uuid):
    raw = redis_client.get(CHANNEL_LIMIT_KEY.format(channel_uuid=channel_uuid))
    raw = raw.decode() if isinstance(raw, bytes) else raw
    if not raw:
        return ""
    limit, _, kept = raw.partition("|")
    started = redis_client.hget(f"live:channel:{channel_uuid}:metadata", "init_time")
    started = started.decode() if isinstance(started, bytes) else started
    try:
        if started and kept and float(kept) < float(started) - LIMIT_BEFORE_START:
            # Somebody else started the channel since: stock's order for them
            return ""
    except ValueError:
        pass
    return limit


def failover_order(redis_client, channel_uuid, alternates, streams):
    """
    Stock failover's next streams, with those within the limit of whoever started the
    channel first: without it a device that started on HD because it cannot play 4K fails
    over onto the 4K stream, which is exactly what it started on HD to avoid. alternates
    are stock's entries ({"stream_id": ...}); streams the channel's Stream objects.
    """
    if not alternates or not redis_client or not _limits_in_use(load_settings()):
        return alternates
    try:
        limit = _limit_of_this_run(redis_client, channel_uuid)
        if not limit:
            return alternates
        from apps.channels.channel_manager import QUALITY_LABELS, quality_of

        by_id = {stream.id: stream for stream in streams}

        def within(entry):
            stream = by_id.get(entry["stream_id"])
            if stream is None:
                return True
            label, rank, _probed = quality_of(stream.name, stream.stream_stats)
            return not label or rank >= QUALITY_LABELS.index(limit)

        # Custom streams (the fallback) stay last, as in ordered_for
        custom = {s.id for s in streams if s.is_custom}
        real = [e for e in alternates if e["stream_id"] not in custom]
        allowed = [e for e in real if within(e)]
        return allowed + [e for e in real if e not in allowed] + [e for e in alternates if e["stream_id"] in custom]
    except Exception as e:
        logger.debug(f"App devices: failover order left as it was for {channel_uuid}: {e}")
        return alternates


def commercial_breaks():
    """Whether recordings here get their commercial breaks marked, and why not."""
    import shutil

    try:
        from core.models import CoreSettings

        enabled = bool(CoreSettings.get_dvr_comskip_enabled())
        mode = CoreSettings.get_dvr_comskip_mode()
    except Exception:
        enabled, mode = False, "cut"
    installed = shutil.which("comskip") is not None
    return {"installed": installed, "enabled": enabled, "mode": mode,
            "marks": installed and mode == "mark"}


def capabilities():
    """What an app may rely on here, for GET /api/core/capabilities/."""
    try:
        from version import __build__, __version__
    except Exception:
        __build__, __version__ = "", ""
    settings = load_settings()
    return {
        "build": __build__,
        "version": __version__,
        "app_integration": 1,
        "devices": settings["devices"],
        "multiview": settings["devices"],
        "switch_hints": settings["switch_hints"],
        "reports": settings["reports"],
        "outside_max_quality": settings["outside_max_quality"],
        "report_url": "/api/core/app-reports/",
        # A stutter is only acted on for a device the server recognises
        "stall_switch": bool(settings["devices"] and settings["stall_switch"]),
        "stall_url": "/api/core/app-stall/",
        "own_stream": settings["own_stream"],
        # Only for a device the server recognises
        "fast_failover": bool(settings["devices"] and settings["fast_failover"]),
        # X-Dispatch-Alternatives on each stream response (app_alternatives)
        "alternatives": bool(settings["devices"] and settings["alternatives"]),
        # "Wrong guide? Choose another" (app_guides); any device, recognised or not
        "guide_choice": settings["guide_choice"],
        "guide_choice_url": "/api/core/app-guide/",
        # Commercial breaks marked by Comskip: custom_properties.comskip.breaks on a recording,
        # when Comskip is installed here, switched on and in "mark" mode (it never cuts then)
        "commercial_breaks": commercial_breaks(),
        # Server rewind (rewind.py): a TV may pause and rewind on the server's recording
        "rewind": settings["rewind"],
        "rewind_url": "/api/channels/rewind/",
        # Generated captions (captions/live.py), and look back moving another viewer
        "captions_url": "/api/channels/captions/live/",
        "look_back_priority": settings["look_back_priority"],
        "headers": {what: header[5:].replace("_", "-").title() for what, (header, _p) in HEADERS.items()},
        "query_parameters": {what: param for what, (_h, param) in HEADERS.items()},
    }


def announce_guides_changed(channel_ids, source):
    """
    Tell the apps listening on Dispatcharr's socket that these channels' guides changed:
    the same `channels_changed` message Show Groups sends, with `"guide": true`, so arrTV
    reloads those channels' guide link and their programmes at once instead of at its
    10-minute lineup check (which never fetched the programmes of a channel whose guide
    changed). Sent only while an arrTV switch is on: nothing else listens for it, and off
    is stock. Never raises: a message that cannot be sent must not undo a guide change.
    """
    try:
        settings = load_settings()
        if not (settings.get("devices") or settings.get("guide_choice")):
            return 0
        from apps.channels.models import Channel
        from core.utils import send_websocket_update

        uuids = [str(u) for u in Channel.objects.filter(id__in=list(channel_ids)).values_list("uuid", flat=True)]
        if not uuids:
            return 0
        send_websocket_update("updates", "update", {
            "type": "channels_changed", "source": source, "channels": uuids,
            "changes": len(uuids), "guide": True,
        })
        return len(uuids)
    except Exception as e:
        logger.debug(f"App devices: could not announce changed guides: {e}")
        return 0
