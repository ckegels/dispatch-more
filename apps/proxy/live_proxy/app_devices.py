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
DEFAULTS = {"devices": False, "switch_hints": False, "reports": False}

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
}


def load_settings():
    now = time.monotonic()
    if _HELD["value"] is not None and now - _HELD["at"] < HELD_SECONDS:
        return dict(_HELD["value"])
    values = dict(DEFAULTS)
    try:
        from core.models import CoreSettings

        stored = CoreSettings.objects.filter(key=SETTINGS_KEY).first()
        if stored and isinstance(stored.value, dict):
            values.update({k: bool(stored.value[k]) for k in DEFAULTS if k in stored.value})
    except Exception as e:
        logger.debug(f"App devices: could not read the settings: {e}")
    _HELD.update(at=now, value=dict(values))
    return values


def save_settings(given):
    from core.models import CoreSettings

    values = {**load_settings(), **{k: bool(given[k]) for k in DEFAULTS if k in (given or {})}}
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
        "report_url": "/api/core/app-reports/",
        "headers": {what: header[5:].replace("_", "-").title() for what, (header, _p) in HEADERS.items()},
        "query_parameters": {what: param for what, (_h, param) in HEADERS.items()},
    }
