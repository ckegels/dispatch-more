"""One arrTV device at a time, for the logins the admin chose (Settings → arrTV).

Some logins are meant for one screen: a family member's account, one shared with a friend. An
admin who chooses them here has arrTV play on one device of that login at a time: live TV,
look back (catch-up) and movies and series alike -- whatever holds a provider connection, which
is what one device at a time is for. When a second device starts any of them, the first one's
streams are closed and it is told why: the next request it makes -- arrTV reconnects at once when a stream ends -- is
refused with 409 and `device_replaced`, naming the device that took over, and arrTV shows that
instead of reconnecting. It plays again when its viewer presses "Watch here", which sends
`X-Dispatch-Take-Over` and moves the stream back the same way.

Why the refusal and not just the close: a closed stream looks to the player like any stream
that ended, and arrTV rightly reconnects then. Without the refusal the two devices would take
the stream from each other at every reconnect, forever. The device that was replaced is
remembered until it takes over or the device that replaced it stops watching, so a TV that was
simply left on gets its stream back by itself the next time it asks once the other is done.

The replaced device is also told at once, on Dispatcharr's socket (`device_replaced`, only to
that login's sockets): a closed stream reaches the screen only once the player has played
what it had buffered, ten or twenty seconds on a TV and more with its own Live Rewind, and on
the real installation that looked like nothing was stopped at all.

Only devices that said who they are (app_devices, "Recognise each arrTV device") take part:
without that the server cannot tell two TVs on one login apart. Recordings are never closed,
nor anyone else's viewing of a shared channel, nor any other player on that login. Off -- no
login chosen -- is stock: one setting read, and only for a request from a declared device.
"""

import json
import logging
import time

from . import app_devices

logger = logging.getLogger("live_proxy")

# The device that replaced this one, while it is still remembered: {"by": key, "name": ...}
REPLACED_KEY = "live:app_devices:replaced:{user_id}:{device}"
# A day at most: by then nobody is waiting on that TV for an answer
REPLACED_TTL = 24 * 3600
# The new device is not yet a client of its channel while that channel starts (seconds, for a
# cold one), and the replaced one reconnects within a second of being closed: for this long it
# is refused whether or not the new device is seen watching yet, or it would slip back in
STARTING_SECONDS = 60

# Which device a look back session or a movie connection is from. Live clients carry their
# device in Dispatcharr's own client record (record_client_viewer); look back and movies have no
# such field, so the device each request said is kept here by the session it streams under
CLIENT_DEVICE_KEY = "live:app_devices:client_device:{client_id}"
CLIENT_DEVICE_TTL = 12 * 3600

# What arrTV shows when the device that took over never said its name
UNNAMED = "another device"


def chosen_users(settings=None):
    """The ids of the logins held to one device, as a set of strings."""
    settings = settings or app_devices.load_settings()
    return {u for u in str(settings.get("one_device_users") or "").split(",") if u}


def applies_to(viewer):
    """
    The login this request is held to one device under, or None. Only for a device that
    declared itself (its key is app|<user>|<device>), on a login the admin chose.
    """
    if viewer is None or viewer.recording or not viewer.user_id:
        return None
    if not app_devices.is_declared(viewer.server_device):
        return None
    settings = app_devices.load_settings()
    if not settings.get("devices"):
        return None
    return viewer.user_id if str(viewer.user_id) in chosen_users(settings) else None


def _as_str(value):
    return value.decode() if isinstance(value, bytes) else value


def _replaced_key(user_id, device_key):
    # The device key already carries the login; the user id in front keeps one login's
    # records together for anyone reading Redis
    return REPLACED_KEY.format(user_id=user_id, device=device_key)


def plain_name(redis_client, device_key):
    """What the device calls itself ("Bedroom TV"), without the login in front."""
    try:
        raw = redis_client.hget(app_devices.NAMES_KEY, device_key)
        if raw:
            return (json.loads(_as_str(raw)).get("name") or "").strip()
    except Exception:
        pass
    return ""


def live_clients_by_device(redis_client, user_id):
    """
    {device key: [(channel uuid, client id), ...]} for the live clients of this login that a
    declared device holds. A recording, a player that did not say which device it is and
    another login's client are left out: none of them is "another arrTV device".
    """
    devices = {}
    for key in redis_client.scan_iter(match="live:channel:*:clients:*", count=1000):
        parts = _as_str(key).split(":")
        if len(parts) != 5:
            continue
        channel_uuid, client_id = parts[2], parts[4]
        client = redis_client.hgetall(_as_str(key)) or {}
        client = {_as_str(k): _as_str(v) for k, v in client.items()}
        device = client.get("server_device") or ""
        if not app_devices.is_declared(device) or client.get("user_id") != str(user_id):
            continue
        devices.setdefault(device, []).append((channel_uuid, client_id))
    return devices


def note_client(redis_client, client_id, device_key):
    """Remember that this look back session or movie connection is this device's."""
    if redis_client is None or not client_id:
        return
    try:
        redis_client.set(CLIENT_DEVICE_KEY.format(client_id=client_id), device_key, ex=CLIENT_DEVICE_TTL)
    except Exception as e:
        logger.debug(f"One device: could not note {client_id}: {e}")


def streams_by_device(redis_client, user_id):
    """
    {device key: [(kind, channel or content, client id), ...]}: everything of this login that
    a declared device holds a provider connection with. kind is "live", "timeshift" (look back)
    or "vod" (a movie or an episode). Stock lists look back and movies by login
    (get_user_active_connections, which the user's own stream limit counts with); the device
    each is from is what note_client kept.
    """
    devices = {
        device: [("live", channel, client) for channel, client in clients]
        for device, clients in live_clients_by_device(redis_client, user_id).items()
    }
    from apps.proxy.utils import get_user_active_connections

    for conn in get_user_active_connections(user_id):
        if conn.get("type") not in ("timeshift", "vod"):
            continue
        device = _as_str(redis_client.get(CLIENT_DEVICE_KEY.format(client_id=conn["client_id"])))
        if app_devices.is_declared(device):
            devices.setdefault(device, []).append((conn["type"], str(conn["media_id"]), conn["client_id"]))
    return devices


def _close_stream(redis_client, kind, media, client_id, device_key, keep_channel):
    """Close one stream of the device being replaced, the way stock stops one of its kind."""
    if kind == "timeshift":
        # What the admin's Stats page does for one look back viewer
        from apps.proxy.utils import stop_timeshift_client

        stop_timeshift_client(redis_client, media, client_id)
    elif kind == "vod":
        # What the admin's Stats page and the user's stream limit do for one movie
        from apps.proxy.vod_proxy.multi_worker_connection_manager import get_vod_client_stop_key

        redis_client.setex(get_vod_client_stop_key(client_id), 60, "true")
    else:
        _close(redis_client, media, client_id, device_key, keep_channel)


def _close(redis_client, channel_uuid, client_id, device_key, keep_channel):
    """
    Close one client of the device being replaced. A channel only that device watches is
    closed whole, its slot free for the new device at once (as Force Close does); one somebody
    else is on as well, or the one the new device is asking for, keeps running and only this
    device's connection to it ends.
    """
    from apps.proxy.live_proxy.redis_keys import RedisKeys
    from apps.proxy.live_proxy.services.channel_service import ChannelService

    from . import probation

    others = [
        c for c in probation._channel_clients(redis_client, channel_uuid)
        if (c.get("server_device") or "") != device_key
    ]
    if keep_channel or others or probation._dispatcharr_is_stopping(redis_client, channel_uuid):
        ChannelService.stop_client(channel_uuid, client_id)
        return
    probation._stop_channel_now(redis_client, channel_uuid, hold_slot=False)
    # The user's own stream limit is checked right after this, before the background stop
    # has removed the client records: they would still count and refuse the new device
    for other in redis_client.smembers(RedisKeys.clients(channel_uuid)) or ():
        redis_client.delete(RedisKeys.client_metadata(channel_uuid, _as_str(other)))


def tell(user_id, device_key, by_name):
    """
    Tell the replaced device now, on the socket arrTV keeps open, rather than when its buffer
    runs dry: `device_replaced` with the device's own id (which arrTV compares with its own)
    and the name of the device that took over. Only the login's own sockets receive it
    (dispatcharr.consumers.user_may_receive_update). Never raises: the refusal still tells it.
    """
    try:
        from core.utils import send_websocket_update

        send_websocket_update("updates", "update", {
            "type": "device_replaced",
            "user_id": user_id,
            "device": str(device_key).split("|", 2)[-1],
            "by": by_name or UNNAMED,
        })
    except Exception as e:
        logger.debug(f"One device: could not tell {device_key}: {e}")


def refusal(name):
    """The answer a replaced device gets until it takes over: what arrTV reads and shows."""
    name = name or UNNAMED
    return {
        "error": f"Playing on another device: {name}",
        "device_replaced": True,
        "by": name,
    }


def check(redis_client, request, viewer, channel_uuids, client_id=None):
    """
    Called for each live, look back and movie stream request before the user's own stream
    limit. Returns the body of a 409 refusal when this device was replaced and has not taken
    over, or None to go on -- after closing the login's other devices' streams when there are
    any.

    channel_uuids: for live, the channel asked for and the stream of its own it may be given
    instead (app_own_streams); another device on that very channel only loses its connection to
    it, so the channel goes on running for this one. client_id: for look back and movies, the
    session this request streams under, kept as this device's (note_client).
    """
    user_id = applies_to(viewer)
    if user_id is None or redis_client is None:
        return None
    me = viewer.server_device
    try:
        if client_id:
            note_client(redis_client, client_id, me)
        holding = streams_by_device(redis_client, user_id)
        others = {device: clients for device, clients in holding.items() if device != me}
        mine = _replaced_key(user_id, me)
        replaced = redis_client.get(mine)
        if replaced:
            by = json.loads(_as_str(replaced))
            if app_devices.declared_take_over(request):
                redis_client.delete(mine)
            elif by.get("by") in others or time.time() - float(by.get("at") or 0) < STARTING_SECONDS:
                logger.info(
                    f"One device: {app_devices.device_name(redis_client, me) or me} was replaced "
                    f"by {by.get('name') or by.get('by')}; refused until it takes over"
                )
                return refusal(by.get("name"))
            else:
                # The device that took over is no longer watching: this one may again
                redis_client.delete(mine)
        if not others:
            return None
        my_name = plain_name(redis_client, me)
        asked = {str(c) for c in channel_uuids if c}
        for device, clients in others.items():
            logger.info(
                f"One device: {app_devices.device_name(redis_client, me) or me} starts a stream; "
                f"closing {len(clients)} stream(s) of {app_devices.device_name(redis_client, device) or device}"
            )
            redis_client.set(
                _replaced_key(user_id, device),
                json.dumps({"by": me, "name": my_name, "at": time.time()}),
                ex=REPLACED_TTL,
            )
            for kind, media, their_client in clients:
                try:
                    _close_stream(redis_client, kind, media, their_client, device, keep_channel=media in asked)
                except Exception as e:
                    logger.warning(f"One device: could not close {kind} {their_client} on {media}: {e}")
            tell(user_id, device, my_name)
            try:
                from . import probation

                probation.record_event(
                    redis_client, viewer, "replaced another device (one device at a time)",
                    channel=",".join(sorted({m for _k, m, _c in clients})), result="closed",
                )
            except Exception:
                pass
    except Exception as e:
        # Never costs the new device its stream: at worst both play, as without the switch
        logger.warning(f"One device: could not check user {user_id}: {e}", exc_info=True)
    return None


def check_request(redis_client, request, user, client_id):
    """
    check() for a look back or movie request: the viewer as the live proxy sees it (the device
    the app declared, with the login), and the session the request streams under.
    """
    if user is None or not app_devices.declared_device(request):
        return None
    from dispatcharr.utils import get_client_ip

    from . import probation

    viewer = probation.viewer_from_request(request, user, get_client_ip(request), redis_client)
    return check(redis_client, request, viewer, (), client_id=client_id)
