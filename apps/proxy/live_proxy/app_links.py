"""arrTV devices watching together (Settings → arrTV, "Watch together").

The user (2026-10-17): when a second TV of the same login starts watching, offer to join, and
once joined, whichever of the two changes channel, the other follows. Both are then always on
the same channel, which is one provider connection however many TVs watch it -- so it fits with
one device at a time (app_one_device), which only acts on a request that needs a connection of
its own: linked devices never replace each other.

**Other people in the house** (v266, `link_users`): the users chosen there may also join each
other's devices, when both devices are in the same house as the server sees them -- both on
the home networks, or both coming from the same address (a remote site over the VPN is one
address; a phone on its own over the VPN is its own) -- and only on a channel the one joining
may watch himself. Between people it is not two-way: the user's rules (2026-10-17),

- joining another person's device asks that device first (`link_request`): Allow / Decline,
  and no answer within ten seconds is Allow;
- the one joined leads: the group's channel is the leader's;
- either side changing channel asks the other (`link_follow_request`, "the 10 seconds ask in
  both directions"): follow, or stay; no answer within ten seconds is follow, which the app does
  by tuning the channel. Staying (`stay`) parts them: the leader staying takes the other out,
  the other staying leaves;
- either side can end it: the one who joined leaves, the leader can take him out (`remove`).

**A group** is kept under its leader's login id, the same keys as before for a login's own
devices, so a group of one login is exactly what it was:

- LINK_KEY: {device key: json {"since": ...}} -- the devices watching together;
- CHANNEL_KEY: the channel the group is on, as the last live request of a leader asked;
- GROUP_KEY: for a device from another login, the group it is in (the leader's login id).

A device's group is GROUP_KEY, or else its own login's. Its leaders are the members of the
group's own login; every one of one login is a leader, so between one's own devices whoever
changes channel is followed. Messages go on Dispatcharr's socket to the members' logins only
(`user_id`, and `user_ids` when the group spans several, see dispatcharr.consumers
.user_may_receive_update). A member that starts a look back or a movie leaves the group (live
channels only, for now).

In step: each linked device reports, every few seconds, the stream timestamp (PTS, 90 kHz) of
the frame it shows and when (report_position). Both get the same bytes from the channel's buffer,
so the timestamps compare; the answer gives a device the others' latest reports. Only devices on
the same running channel compare: a stream of its own has other timestamps.

Only declared devices (app_devices, "Recognise each arrTV device"), only while switched on: off
is stock, a header lookup at most.
"""

import json
import logging
import time

from . import app_devices

logger = logging.getLogger("live_proxy")

LINK_KEY = "live:app_devices:links:{user_id}"
CHANNEL_KEY = "live:app_devices:link_channel:{user_id}"
# The group a device of another login is in: the leader's login id
GROUP_KEY = "live:app_devices:link_group:{device}"
# Asked of a device of another login: {device id of the one asking: json}
REQUEST_KEY = "live:app_devices:link_requests:{device}"
# What a device asks before it gets an answer, and how long it waits for one: long enough for a
# device whose socket is down to find it in its status (asked every 10 s) and answer
REQUEST_TTL = 45
# A group nobody has changed channel in or asked about for this long is forgotten
LINK_TTL = 12 * 3600
# In step: the latest position report of each linked device, {device key: json}
POSITION_KEY = "live:app_devices:link_positions:{user_id}"
# A report older than this says nothing about where that device is now
POSITION_FRESH_SECONDS = 30


def enabled():
    settings = app_devices.load_settings()
    return bool(settings.get("devices") and settings.get("link_devices"))


def people(settings=None):
    """The ids of the users who may watch together with other users in the house, as strings."""
    settings = settings or app_devices.load_settings()
    return {u for u in str(settings.get("link_users") or "").split(",") if u}


def _as_str(value):
    return value.decode() if isinstance(value, bytes) else value


def _device_id(device_key):
    """The id the app sent, from a device key (app|<user>|<device>)."""
    return str(device_key).split("|", 2)[-1]


def _user_of(device_key):
    """The login id a device key (app|<user>|<device>) is of, as a string."""
    parts = str(device_key).split("|", 2)
    return parts[1] if len(parts) == 3 else ""


def group_of(redis_client, device_key):
    """The group this device is in, or would lead: its own login's, unless it joined another's."""
    try:
        joined = _as_str(redis_client.get(GROUP_KEY.format(device=device_key)))
    except Exception:
        joined = None
    return joined or _user_of(device_key)


def members(redis_client, user_id):
    """The device keys of a group (by its leader's login id), as a set."""
    if redis_client is None or not user_id:
        return set()
    try:
        return {_as_str(k) for k in (redis_client.hgetall(LINK_KEY.format(user_id=user_id)) or {})}
    except Exception:
        return set()


def linked_with(redis_client, user_id, device_key):
    """The other devices this one watches together with, as a set of device keys."""
    group = members(redis_client, group_of(redis_client, device_key))
    return group - {device_key} if device_key in group else set()


def leads(redis_client, device_key):
    """Whether this device's channel changes are followed without asking: one of the group's login."""
    return _user_of(device_key) == str(group_of(redis_client, device_key))


def _touch(redis_client, group):
    keys = [LINK_KEY.format(user_id=group), CHANNEL_KEY.format(user_id=group)]
    # Another person's device keeps its way to the group as long as the group lasts
    keys += [GROUP_KEY.format(device=d) for d in members(redis_client, group) if _user_of(d) != str(group)]
    for key in keys:
        try:
            redis_client.expire(key, LINK_TTL)
        except Exception:
            pass


def channel_of(redis_client, user_id):
    """The channel a group (by its leader's login id) is on."""
    try:
        return _as_str(redis_client.get(CHANNEL_KEY.format(user_id=user_id))) or None
    except Exception:
        return None


def _send(group, devices, message):
    """A socket message for these devices: to the group's login, and to every other login among them."""
    from core.utils import send_websocket_update

    users = sorted({_user_of(d) for d in devices} | {str(group)})
    body = {**message, "user_id": int(group) if str(group).isdigit() else group}
    if len(users) > 1:
        body["user_ids"] = [int(u) if u.isdigit() else u for u in users]
    send_websocket_update("updates", "update", body)


def _channel_names(uuids):
    try:
        from apps.channels.models import Channel

        return {str(u): n for u, n in Channel.objects.filter(uuid__in=list(uuids)).values_list("uuid", "name")}
    except Exception:
        return {}


def _user_names(ids):
    try:
        from apps.accounts.models import User

        return {
            str(i): (first or name)
            for i, name, first in User.objects.filter(id__in=[int(i) for i in ids if str(i).isdigit()])
            .values_list("id", "username", "first_name")
        }
    except Exception:
        return {}


def may_watch(user_id, channel_uuid):
    """Whether this login may watch this channel, by Dispatcharr's own rule (its level, its channel profiles)."""
    try:
        from apps.accounts.models import User
        from apps.channels.models import Channel

        user = User.objects.get(id=int(user_id))
        if user.user_level >= 10:
            return Channel.objects.filter(uuid=channel_uuid).exists()
        channels = Channel.objects.filter(uuid=channel_uuid, user_level__lte=user.user_level)
        if user.channel_profiles.count():
            channels = channels.filter(
                channelprofilemembership__enabled=True,
                channelprofilemembership__channel_profile__in=user.channel_profiles.all(),
            )
        return channels.exists()
    except Exception:
        return False


def house_of(ip, settings=None):
    """
    Which house an address is in, as the server sees it: "home" on the home networks, else the
    address itself -- a remote site over the VPN is one address, a phone on the road its own.
    """
    settings = settings or app_devices.load_settings()
    if ip and app_devices._at_home(ip, settings.get("home_networks")):
        return "home"
    return f"ip:{ip}" if ip else ""


def _devices_watching(redis_client, users):
    """{device key: {"channels": set, "ip": address}} for the live clients of these logins' declared devices."""
    from .redis_keys import RedisKeys  # noqa: F401  (the key shape below is RedisKeys.client_metadata's)

    found = {}
    for key in redis_client.scan_iter(match="live:channel:*:clients:*", count=1000):
        parts = _as_str(key).split(":")
        if len(parts) != 5:
            continue
        client = {_as_str(k): _as_str(v) for k, v in (redis_client.hgetall(_as_str(key)) or {}).items()}
        device = client.get("server_device") or ""
        if not app_devices.is_declared(device) or client.get("user_id") not in users:
            continue
        entry = found.setdefault(device, {"channels": set(), "ip": client.get("ip_address") or ""})
        entry["channels"].add(parts[2])
    return found


def offers(redis_client, user_id, device_key, ip=None):
    """
    The devices this one may join, each watching a live channel now: the login's other arrTV
    devices, and -- for the users chosen to watch with other people -- those people's devices
    in the same house, on a channel this login may watch (with "user" and "user_id"). A
    multiview's tiles are left out (no one channel to follow).
    """
    from . import app_one_device

    settings = app_devices.load_settings()
    mine = str(user_id)
    others = people(settings) - {mine} if mine in people(settings) and ip else set()
    watching = {}
    for device, clients in app_one_device.live_clients_by_device(redis_client, user_id).items():
        if device != device_key:
            watching[device] = {c for c, _ in clients}
    across = _devices_watching(redis_client, others) if others else {}
    here = house_of(ip, settings) if across else ""
    for device, entry in across.items():
        if house_of(entry["ip"], settings) == here:
            watching[device] = entry["channels"]
    names = _channel_names({c for chans in watching.values() for c in chans})
    users = _user_names({_user_of(d) for d in watching} - {mine})
    result = []
    for device, channels in watching.items():
        if len(channels) != 1:
            continue
        (channel_uuid,) = channels
        offer = {
            "device": _device_id(device),
            "name": app_one_device.plain_name(redis_client, device) or app_one_device.UNNAMED,
            "channel": channel_uuid,
            "channel_name": names.get(channel_uuid, ""),
        }
        if _user_of(device) != mine:
            if not may_watch(user_id, channel_uuid):
                continue
            offer.update(user_id=_user_of(device), user=users.get(_user_of(device), ""))
        result.append(offer)
    return result


def _link(redis_client, group, device_key, other_key, channel):
    """Put both devices in the group, on its channel."""
    # Out of whatever group the joining device was in first
    if group_of(redis_client, device_key) != str(group):
        _drop(redis_client, device_key, tell_others=True)
    now = json.dumps({"since": time.time()})
    link_key = LINK_KEY.format(user_id=group)
    redis_client.hset(link_key, other_key, now)
    redis_client.hset(link_key, device_key, now)
    for key in (device_key, other_key):
        if _user_of(key) != str(group):
            redis_client.set(GROUP_KEY.format(device=key), str(group), ex=LINK_TTL)
    redis_client.set(CHANNEL_KEY.format(user_id=group), channel, ex=LINK_TTL)
    _touch(redis_client, group)
    # Joined: whatever replaced either device before is now what it watches together with
    from . import app_one_device

    for key in (device_key, other_key):
        redis_client.delete(app_one_device._replaced_key(_user_of(key), key))


def join(redis_client, user_id, device_key, other_device_id, ip=None):
    """
    This device joins the other one (and the group it is in): returns {"channel", "name"} of
    what it is watching, {"pending": True, ...} when the other is another person's and has
    been asked first, or None when it is not watching a channel now (or may not be joined).
    """
    offer = next((o for o in offers(redis_client, user_id, device_key, ip) if o["device"] == other_device_id), None)
    if offer is None:
        return None
    other_key = app_devices.device_key(offer.get("user_id") or user_id, other_device_id)
    if offer.get("user_id"):
        # Another person's device: asked first, answered by it (answer)
        names = _user_names({str(user_id)})
        request = {
            "device": _device_id(device_key),
            "key": device_key,
            "name": app_devices.device_name(redis_client, device_key) or _device_id(device_key),
            "plain": _plain(redis_client, device_key),
            "user": names.get(str(user_id), ""),
            "channel": offer["channel"],
            "channel_name": offer["channel_name"],
            "at": time.time(),
        }
        asked = REQUEST_KEY.format(device=other_key)
        redis_client.hset(asked, request["device"], json.dumps(request))
        redis_client.expire(asked, REQUEST_TTL)
        try:
            _send(group_of(redis_client, other_key), {other_key, device_key}, {
                "type": "link_request",
                "by": request["device"],
                "name": _plain(redis_client, device_key),
                "user": request["user"],
                "channel": offer["channel"],
                "channel_name": offer["channel_name"],
                "devices": [other_device_id],
            })
        except Exception as e:
            logger.debug(f"Watch together: could not ask {other_key}: {e}")
        logger.info(
            f"Watch together: {request['name']} asks to join "
            f"{app_devices.device_name(redis_client, other_key) or other_key} on {offer['channel']}"
        )
        return {"pending": True, "name": offer["name"], "user": offer.get("user", ""),
                "channel": offer["channel"], "channel_name": offer["channel_name"]}
    group = group_of(redis_client, other_key)
    _link(redis_client, group, device_key, other_key, offer["channel"])
    logger.info(
        f"Watch together: {app_devices.device_name(redis_client, device_key) or device_key} joins "
        f"{app_devices.device_name(redis_client, other_key) or other_key} on {offer['channel']}"
    )
    # The devices joined are linked too, and have to know it: only a linked device reports its
    # position, and on the real server (2026-10-17) the TV the phone joined never did -- the
    # phone heard nobody, and the two stayed half a minute apart
    try:
        everyone = members(redis_client, group)
        _send(group, everyone, {
            "type": "link_joined",
            "by": _device_id(device_key),
            "devices": sorted(_device_id(d) for d in everyone - {device_key}),
        })
    except Exception as e:
        logger.debug(f"Watch together: could not tell the joined device: {e}")
    return {"channel": offer["channel"], "name": offer["name"], "channel_name": offer["channel_name"]}


def _plain(redis_client, device_key):
    from . import app_one_device

    return app_one_device.plain_name(redis_client, device_key) or app_one_device.UNNAMED


def answer(redis_client, user_id, device_key, asking_device_id, allow):
    """
    This device answers another person's request to join it: allowed, the two are linked (and
    the one asking is told the channel, `link_accepted`); declined, he is told (`link_declined`).
    False when there was no such request (it ran out).
    """
    asked = REQUEST_KEY.format(device=device_key)
    raw = redis_client.hget(asked, asking_device_id)
    if not raw:
        return False
    redis_client.hdel(asked, asking_device_id)
    request = json.loads(_as_str(raw))
    guest = request["key"]
    group = group_of(redis_client, device_key)
    if not allow:
        _send(group, {guest}, {"type": "link_declined", "by": _device_id(device_key),
                               "name": _plain(redis_client, device_key), "devices": [asking_device_id]})
        logger.info(f"Watch together: {request['name']} was not let in")
        return True
    from . import app_one_device

    channels = {c for c, _ in app_one_device.live_clients_by_device(redis_client, user_id).get(device_key, [])}
    channel = next(iter(channels)) if len(channels) == 1 else request["channel"]
    if not may_watch(_user_of(guest), channel):
        _send(group, {guest}, {"type": "link_declined", "by": _device_id(device_key),
                               "name": _plain(redis_client, device_key), "devices": [asking_device_id],
                               "reason": "not_allowed"})
        return True
    _link(redis_client, group, guest, device_key, channel)
    everyone = members(redis_client, group)
    _send(group, everyone, {"type": "link_accepted", "by": _device_id(device_key),
                            "name": _plain(redis_client, device_key), "channel": channel,
                            "devices": [asking_device_id]})
    # The others hear it the way they hear any join: tune afresh, in step with the newcomer
    _send(group, everyone, {"type": "link_joined", "by": asking_device_id,
                            "devices": sorted(_device_id(d) for d in everyone - {guest})})
    logger.info(f"Watch together: {request['name']} joins {_plain(redis_client, device_key)} on {channel}")
    return True


def _drop(redis_client, device_key, tell_others, reason=""):
    """
    Take this device out of its group; a group of one, or without its leader, ends, and the
    ones left are told -- with the reason and who, when this one left for one ("stayed").
    """
    group = group_of(redis_client, device_key)
    link_key = LINK_KEY.format(user_id=group)
    if device_key not in members(redis_client, group):
        redis_client.delete(GROUP_KEY.format(device=device_key))
        return False
    redis_client.hdel(link_key, device_key)
    redis_client.delete(GROUP_KEY.format(device=device_key))
    left = members(redis_client, group)
    leaders = {d for d in left if _user_of(d) == str(group)}
    if len(left) < 2 or not leaders:
        redis_client.delete(link_key)
        redis_client.delete(CHANNEL_KEY.format(user_id=group))
        redis_client.delete(POSITION_KEY.format(user_id=group))
        for other in left:
            redis_client.delete(GROUP_KEY.format(device=other))
        if left and tell_others:
            message = {"type": "link_ended", "devices": sorted(_device_id(d) for d in left)}
            if reason:
                message.update(reason=reason, by=_device_id(device_key), name=_plain(redis_client, device_key))
            _send(group, left | {device_key}, message)
    return True


def leave(redis_client, user_id, device_key, reason=""):
    """
    This device stops watching together (its viewer chose to, or it left the app, or -- another
    person's group -- it stayed on its channel when the leader changed, `reason` "stayed"); a
    group of one is no group, and the one left is told (`link_ended`, those logins only), so it
    stops offering "Stop watching together" and comparing positions with nobody. It plays on.
    """
    if redis_client is None or not user_id:
        return
    try:
        if _drop(redis_client, device_key, tell_others=True, reason=reason):
            logger.info(f"Watch together: {app_devices.device_name(redis_client, device_key) or device_key} left")
    except Exception as e:
        logger.debug(f"Watch together: could not take {device_key} out: {e}")


def remove(redis_client, user_id, device_key, other_device_id, reason="removed"):
    """
    A leader takes a device of another person out of its group (Disconnect, or Stay when that
    one changed channel); it is told (`link_ended` with the reason) and plays on. False when
    this device does not lead, or the other is not in its group.
    """
    if not leads(redis_client, device_key):
        return False
    group = group_of(redis_client, device_key)
    other = next((d for d in members(redis_client, group) if _device_id(d) == other_device_id), None)
    if other is None or _user_of(other) == str(group):
        return False
    _drop(redis_client, other, tell_others=True)
    try:
        _send(group, {other}, {"type": "link_ended", "devices": [other_device_id], "reason": reason,
                               "by": _device_id(device_key), "name": _plain(redis_client, device_key)})
    except Exception as e:
        logger.debug(f"Watch together: could not tell {other}: {e}")
    logger.info(f"Watch together: {_plain(redis_client, device_key)} took {_plain(redis_client, other)} out ({reason})")
    return True


def state(redis_client, user_id, device_key, ip=None):
    """What the app shows: linked or not, the channel, whether it leads, who is in it, and what it may join."""
    group = group_of(redis_client, device_key)
    linked = linked_with(redis_client, user_id, device_key)
    users = _user_names({_user_of(d) for d in linked} - {str(user_id)})
    together = [
        {
            "device": _device_id(d),
            "name": _plain(redis_client, d),
            "user": users.get(_user_of(d), "") if _user_of(d) != str(user_id) else "",
            "leads": _user_of(d) == str(group),
        }
        for d in sorted(linked)
    ]
    return {
        "linked": bool(linked),
        "channel": channel_of(redis_client, group) if linked else None,
        "leads": leads(redis_client, device_key),
        "together": together,
        "offers": offers(redis_client, user_id, device_key, ip),
        # Asked of this device and not answered yet: a device whose socket is down finds them here
        "requests": requests_for(redis_client, device_key),
    }


def requests_for(redis_client, device_key):
    """The requests to join this device that wait for its answer, oldest first."""
    try:
        raw = redis_client.hgetall(REQUEST_KEY.format(device=device_key)) or {}
    except Exception:
        return []
    now = time.time()
    found = []
    for value in raw.values():
        try:
            request = json.loads(_as_str(value))
        except (TypeError, ValueError):
            continue
        if now - float(request.get("at") or 0) > REQUEST_TTL:
            continue
        found.append({
            "by": request.get("device", ""),
            "name": request.get("plain") or request.get("name", ""),
            "user": request.get("user", ""),
            "channel_name": request.get("channel_name", ""),
            "at": request.get("at"),
        })
    return sorted(found, key=lambda r: r.get("at") or 0)


def report_position(redis_client, user_id, device_key, report):
    """
    Keep this device's report ({"pts": 90 kHz, "at": epoch ms}) and answer the others' fresh
    reports, each with the device's name: those on the very channel this device's live client
    is on now. Nothing for a device that is not linked.
    """
    others = linked_with(redis_client, user_id, device_key)
    if not others:
        return []
    try:
        pts = int(report.get("pts"))
        at = int(report.get("at"))
    except (TypeError, ValueError):
        return []
    group = group_of(redis_client, device_key)
    watching = _devices_watching(redis_client, {_user_of(d) for d in others | {device_key}})
    mine = watching.get(device_key, {}).get("channels", set())
    if len(mine) != 1:
        return []
    key = POSITION_KEY.format(user_id=group)
    redis_client.hset(key, device_key, json.dumps({"pts": pts, "at": at, "channel": next(iter(mine))}))
    redis_client.expire(key, LINK_TTL)
    now_ms = time.time() * 1000
    answer_ = []
    for other in others:
        raw = redis_client.hget(key, other)
        if not raw:
            continue
        said = json.loads(_as_str(raw))
        theirs = watching.get(other, {}).get("channels", set())
        if theirs != mine or said.get("channel") not in mine:
            continue
        if now_ms - float(said.get("at") or 0) > POSITION_FRESH_SECONDS * 1000:
            continue
        answer_.append({"device": _device_id(other), "name": _plain(redis_client, other),
                        "pts": said["pts"], "at": said["at"]})
    return answer_


def _not_on(redis_client, devices, channel_uuid):
    """These devices but those already watching the channel."""
    if not devices:
        return set()
    watching = _devices_watching(redis_client, {_user_of(d) for d in devices})
    return {d for d in devices if channel_uuid not in watching.get(d, {}).get("channels", set())}


def _ask_to_follow(redis_client, group, device, asked, channel_uuid):
    """Ask these devices whether to follow this one to the channel: Follow, or Stay (and leave)."""
    names = _channel_names({channel_uuid})
    _send(group, asked | {device}, {
        "type": "link_follow_request",
        "channel": channel_uuid,
        "channel_name": names.get(channel_uuid, ""),
        "by": _device_id(device),
        "name": _plain(redis_client, device),
        "user": _user_names({_user_of(device)}).get(_user_of(device), ""),
        "devices": sorted(_device_id(d) for d in asked),
    })


def mirror(redis_client, request, viewer, channel_uuid):
    """
    A live stream request of a declared device: when it watches together with others and asks
    for another channel than the group's, a leader's is the group's channel now. The leader's
    own login's devices follow at once (`linked_channel`); another person's are asked whether
    to follow (`link_follow_request`, the user: "lets do the 10 seconds ask in both directions"),
    and those who may not watch it are taken out. Another person's change asks the leaders the
    same way. Requests for the group's channel tell nobody. Never raises; costs a header lookup
    for every other request.
    """
    if (
        redis_client is None
        or viewer is None
        or viewer.recording
        or viewer.multiview
        or not app_devices.is_declared(viewer.server_device)
        or not viewer.user_id
        or not channel_uuid
    ):
        return False
    try:
        if not enabled():
            return False
        device = viewer.server_device
        others = linked_with(redis_client, viewer.user_id, device)
        if not others:
            return False
        channel_uuid = str(channel_uuid)
        group = group_of(redis_client, device)
        _touch(redis_client, group)
        if channel_of(redis_client, group) == channel_uuid:
            return False
        if not leads(redis_client, device):
            leaders = _not_on(redis_client, {d for d in others if _user_of(d) == str(group)}, channel_uuid)
            if leaders:
                _ask_to_follow(redis_client, group, device, leaders, channel_uuid)
            logger.info(f"Watch together: {_plain(redis_client, device)} changed to {channel_uuid}; the leader is asked")
            return True
        redis_client.set(CHANNEL_KEY.format(user_id=group), channel_uuid, ex=LINK_TTL)
        for other in sorted(others):
            if _user_of(other) != str(group) and not may_watch(_user_of(other), channel_uuid):
                remove(redis_client, viewer.user_id, device, _device_id(other), reason="not_allowed")
        others = linked_with(redis_client, viewer.user_id, device)
        if not others:
            return True
        # One's own devices follow; another person is asked -- unless already there: the leader
        # following the other's channel asked the other to follow it to where it was (2026-10-18)
        own = {o for o in others if _user_of(o) == _user_of(device)}
        asked = _not_on(redis_client, others - own, channel_uuid)
        if own:
            _send(group, own | {device}, {
                "type": "linked_channel",
                "channel": channel_uuid,
                "by": _device_id(device),
                "devices": sorted(_device_id(d) for d in own),
            })
        if asked:
            _ask_to_follow(redis_client, group, device, asked, channel_uuid)
        logger.info(
            f"Watch together: {app_devices.device_name(redis_client, device) or device} "
            f"changed to {channel_uuid}; {len(own)} follow, {len(asked)} asked"
        )
        return True
    except Exception as e:
        logger.debug(f"Watch together: could not mirror: {e}")
        return False
