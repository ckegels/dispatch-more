"""arrTV devices of one login watching together (Settings → arrTV, "Watch together").

The user (2026-10-17): when a second TV of the same login starts watching, offer to join, and
once joined, whichever of the two changes channel, the other follows. Both are then always on
the same channel, which is one provider connection however many TVs watch it -- so it fits with
one device at a time (app_one_device), which only acts on a request that needs a connection of
its own: linked devices never replace each other.

A login has at most one group of linked devices, in Redis:

- LINK_KEY: {device key: json {"since": ...}} -- the devices watching together;
- CHANNEL_KEY: the channel the group is on, as the last live request of a member asked.

When a member asks for another channel, the group's channel is that one and the other members
are told on Dispatcharr's socket (`linked_channel`, to that login's sockets only, see
dispatcharr.consumers.user_may_receive_update); arrTV tunes to it as a remote press would. A
member that starts a look back or a movie leaves the group (live channels only, for now).

In step: each linked device reports, every few seconds, the stream timestamp (PTS, 90 kHz) of
the frame it shows and when (report_position). Both get the same bytes from the channel's buffer,
so the timestamps compare; the answer gives a device the others' latest reports, and the one
that is ahead waits the difference (the user, 2026-10-17: the two were half a minute apart --
each player's own buffer and every stall it ever had add to how far behind live it plays). Only
devices on the same running channel compare: a stream of its own has other timestamps.

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
# A group nobody has changed channel in or asked about for this long is forgotten
LINK_TTL = 12 * 3600
# In step: the latest position report of each linked device, {device key: json}
POSITION_KEY = "live:app_devices:link_positions:{user_id}"
# A report older than this says nothing about where that device is now
POSITION_FRESH_SECONDS = 30


def enabled():
    settings = app_devices.load_settings()
    return bool(settings.get("devices") and settings.get("link_devices"))


def _as_str(value):
    return value.decode() if isinstance(value, bytes) else value


def _device_id(device_key):
    """The id the app sent, from a device key (app|<user>|<device>)."""
    return str(device_key).split("|", 2)[-1]


def members(redis_client, user_id):
    """The device keys of the login's group, as a set."""
    if redis_client is None or not user_id:
        return set()
    try:
        return {_as_str(k) for k in (redis_client.hgetall(LINK_KEY.format(user_id=user_id)) or {})}
    except Exception:
        return set()


def linked_with(redis_client, user_id, device_key):
    """The other devices this one watches together with, as a set of device keys."""
    group = members(redis_client, user_id)
    return group - {device_key} if device_key in group else set()


def _touch(redis_client, user_id):
    for key in (LINK_KEY.format(user_id=user_id), CHANNEL_KEY.format(user_id=user_id)):
        try:
            redis_client.expire(key, LINK_TTL)
        except Exception:
            pass


def channel_of(redis_client, user_id):
    try:
        return _as_str(redis_client.get(CHANNEL_KEY.format(user_id=user_id))) or None
    except Exception:
        return None


def _channel_names(uuids):
    try:
        from apps.channels.models import Channel

        return {str(u): n for u, n in Channel.objects.filter(uuid__in=list(uuids)).values_list("uuid", "name")}
    except Exception:
        return {}


def offers(redis_client, user_id, device_key):
    """
    The login's other arrTV devices watching a live channel now, each with the channel: what
    this device may join. A multiview's tiles are left out (no one channel to follow).
    """
    from . import app_one_device

    watching = {}
    for device, clients in app_one_device.live_clients_by_device(redis_client, user_id).items():
        if device == device_key:
            continue
        for channel_uuid, _client in clients:
            watching.setdefault(device, set()).add(channel_uuid)
    names = _channel_names({c for chans in watching.values() for c in chans})
    result = []
    for device, channels in watching.items():
        if len(channels) != 1:
            continue
        (channel_uuid,) = channels
        result.append({
            "device": _device_id(device),
            "name": app_one_device.plain_name(redis_client, device) or app_one_device.UNNAMED,
            "channel": channel_uuid,
            "channel_name": names.get(channel_uuid, ""),
        })
    return result


def join(redis_client, user_id, device_key, other_device_id):
    """
    This device joins the other one (and the group it is in): returns {"channel", "name"} of
    what it is watching, or None when it is not watching a channel now.
    """
    other_key = app_devices.device_key(user_id, other_device_id)
    offer = next((o for o in offers(redis_client, user_id, device_key) if o["device"] == other_device_id), None)
    if offer is None:
        return None
    now = json.dumps({"since": time.time()})
    link_key = LINK_KEY.format(user_id=user_id)
    redis_client.hset(link_key, other_key, now)
    redis_client.hset(link_key, device_key, now)
    redis_client.set(CHANNEL_KEY.format(user_id=user_id), offer["channel"], ex=LINK_TTL)
    _touch(redis_client, user_id)
    # Joined: whatever replaced this device before is now what it watches together with
    from . import app_one_device

    redis_client.delete(app_one_device._replaced_key(user_id, device_key))
    redis_client.delete(app_one_device._replaced_key(user_id, other_key))
    logger.info(
        f"Watch together: {app_devices.device_name(redis_client, device_key) or device_key} joins "
        f"{app_devices.device_name(redis_client, other_key) or other_key} on {offer['channel']}"
    )
    # The devices joined are linked too, and have to know it: only a linked device reports its
    # position, and on the real server (2026-10-17) the TV the phone joined never did -- the
    # phone heard nobody, and the two stayed half a minute apart
    try:
        from core.utils import send_websocket_update

        send_websocket_update("updates", "update", {
            "type": "link_joined",
            "user_id": user_id,
            "by": _device_id(device_key),
            "devices": sorted(_device_id(d) for d in members(redis_client, user_id) - {device_key}),
        })
    except Exception as e:
        logger.debug(f"Watch together: could not tell the joined device: {e}")
    return {"channel": offer["channel"], "name": offer["name"], "channel_name": offer["channel_name"]}


def leave(redis_client, user_id, device_key):
    """
    This device stops watching together (its viewer chose to, or it left the app); a group of
    one is no group, and the one left is told (`link_ended`, that login only), so it stops
    offering "Stop watching together" and comparing positions with nobody. It plays on.
    """
    if redis_client is None or not user_id:
        return
    link_key = LINK_KEY.format(user_id=user_id)
    try:
        if device_key not in members(redis_client, user_id):
            return
        redis_client.hdel(link_key, device_key)
        left = members(redis_client, user_id)
        if len(left) < 2:
            redis_client.delete(link_key)
            redis_client.delete(CHANNEL_KEY.format(user_id=user_id))
            redis_client.delete(POSITION_KEY.format(user_id=user_id))
            if left:
                from core.utils import send_websocket_update

                send_websocket_update("updates", "update", {
                    "type": "link_ended",
                    "user_id": user_id,
                    "devices": sorted(_device_id(d) for d in left),
                })
        logger.info(f"Watch together: {app_devices.device_name(redis_client, device_key) or device_key} left")
    except Exception as e:
        logger.debug(f"Watch together: could not take {device_key} out: {e}")


def report_position(redis_client, user_id, device_key, report):
    """
    Keep this device's report ({"pts": 90 kHz, "at": epoch ms}) and answer the others' fresh
    reports, each with the device's name: those on the very channel this device's live client
    is on now. Nothing for a device that is not linked.
    """
    from . import app_one_device

    others = linked_with(redis_client, user_id, device_key)
    if not others:
        return []
    try:
        pts = int(report.get("pts"))
        at = int(report.get("at"))
    except (TypeError, ValueError):
        return []
    watching = app_one_device.live_clients_by_device(redis_client, user_id)
    mine = {c for c, _ in watching.get(device_key, [])}
    if len(mine) != 1:
        return []
    key = POSITION_KEY.format(user_id=user_id)
    redis_client.hset(key, device_key, json.dumps({"pts": pts, "at": at, "channel": next(iter(mine))}))
    redis_client.expire(key, LINK_TTL)
    now_ms = time.time() * 1000
    answer = []
    for other in others:
        raw = redis_client.hget(key, other)
        if not raw:
            continue
        said = json.loads(_as_str(raw))
        theirs = {c for c, _ in watching.get(other, [])}
        if theirs != mine or said.get("channel") not in mine:
            continue
        if now_ms - float(said.get("at") or 0) > POSITION_FRESH_SECONDS * 1000:
            continue
        answer.append({
            "device": _device_id(other),
            "name": app_one_device.plain_name(redis_client, other) or app_one_device.UNNAMED,
            "pts": said["pts"],
            "at": said["at"],
        })
    return answer


def mirror(redis_client, request, viewer, channel_uuid):
    """
    A live stream request of a declared device: when it watches together with others and asks
    for another channel than the group's, that is the group's channel now, and the others are
    told to follow. Their own requests for it then match the group's channel and tell nobody.
    Never raises; costs a header lookup for every other request.
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
        others = linked_with(redis_client, viewer.user_id, viewer.server_device)
        if not others:
            return False
        channel_uuid = str(channel_uuid)
        _touch(redis_client, viewer.user_id)
        if channel_of(redis_client, viewer.user_id) == channel_uuid:
            return False
        redis_client.set(CHANNEL_KEY.format(user_id=viewer.user_id), channel_uuid, ex=LINK_TTL)
        from core.utils import send_websocket_update

        send_websocket_update("updates", "update", {
            "type": "linked_channel",
            "user_id": viewer.user_id,
            "channel": channel_uuid,
            "by": _device_id(viewer.server_device),
            "devices": sorted(_device_id(d) for d in others),
        })
        logger.info(
            f"Watch together: {app_devices.device_name(redis_client, viewer.server_device) or viewer.server_device} "
            f"changed to {channel_uuid}; {len(others)} other device(s) follow"
        )
        return True
    except Exception as e:
        logger.debug(f"Watch together: could not mirror: {e}")
        return False
