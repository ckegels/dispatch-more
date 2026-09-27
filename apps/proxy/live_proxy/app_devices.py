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
# a stream no better than this ("HD", "SD"; "" is no limit) -- see ordered_for
# stall_switch: a channel arrTV stutters on moves to its next stream (see app_stalls)
# own_stream: arrTV gets a stream of its own when a channel plays one it cannot use
# (see app_own_streams)
DEFAULTS = {
    "devices": False, "switch_hints": False, "reports": False,
    "home_networks": "", "outside_max_quality": "", "stall_switch": False,
    "own_stream": False,
}
QUALITY_LIMITS = ("HD", "SD")

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

    values = {**load_settings(), **{k: _as_kind(k, given[k]) for k in DEFAULTS if k in (given or {})}}
    CoreSettings.objects.update_or_create(
        key=SETTINGS_KEY, defaults={"name": "App integration", "value": values}
    )
    _HELD.update(at=0.0, value=None)
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
    """The device the app says it is, or "" -- only while devices are switched on."""
    if not load_settings()["devices"]:
        return ""
    device = _said(request, "device")
    return device if _DEVICE_ID.match(device) else ""


def declared_multiview(request):
    """The multiview session a tile says it belongs to, or ""."""
    if not load_settings()["devices"]:
        return ""
    session = _said(request, "multiview")
    return session if _SESSION_ID.match(session) else ""


def declared_previous_channel(request):
    """
    The channel the app says it is leaving, as its UUID, or "" -- only while switch hints
    are on. Given as the UUID (the /proxy/ts/stream/<uuid> links) or as the channel's number
    id (the Xtream /live/<user>/<pass>/<id> links, where that id is all the app has).
    """
    if not load_settings()["switch_hints"]:
        return ""
    channel = _said(request, "previous")
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
    if not load_settings()["devices"]:
        return ""
    said = _said(request, "max_video").upper().rstrip("P")
    if said in ("FHD", "HD", "SD"):
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
    The best quality this viewer should be given ("HD", "SD"), or "" for none: an arrTV
    device whose address is outside the home networks, when a limit is set. Nothing is
    limited while the home networks are empty -- without them everything would be outside.
    Also the best the device says it can decode (X-Dispatch-Max-Video), and a quality it
    stuttered its way down to where it is now (see app_stalls): the lowest of them.
    """
    settings = load_settings()
    if not is_arrtv(viewer):
        return ""
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
    Keep the limit of the viewer starting a channel, or forget an older one. Called where a
    channel picks its stream, which is only when it starts: a channel is one stream for
    everyone on it, and the one who starts it is who it was chosen for.
    """
    if not redis_client or not _limits_in_use(load_settings()):
        return
    key = CHANNEL_LIMIT_KEY.format(channel_uuid=channel_uuid)
    try:
        limit = quality_limit_for(viewer)
        if limit:
            redis_client.setex(key, CHANNEL_LIMIT_TTL, limit)
        else:
            redis_client.delete(key)
    except Exception as e:
        logger.debug(f"App devices: could not keep the limit of channel {channel_uuid}: {e}")


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
        limit = redis_client.get(CHANNEL_LIMIT_KEY.format(channel_uuid=channel_uuid))
        limit = limit.decode() if isinstance(limit, bytes) else limit
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
        "headers": {what: header[5:].replace("_", "-").title() for what, (header, _p) in HEADERS.items()},
        "query_parameters": {what: param for what, (_h, param) in HEADERS.items()},
    }
