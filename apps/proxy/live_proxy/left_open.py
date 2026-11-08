"""Close a Jellyfin connection that is left open after everybody stopped watching.

To Dispatcharr the viewer of a channel Jellyfin plays is Jellyfin itself, not the person in
front of the TV. When that person leaves, Jellyfin is meant to close its tuner stream; it does
not always, and Dispatcharr then keeps the channel open -- and a provider connection taken --
for a server that reads the stream for nobody. On 2026-11-08 a channel stayed open for almost
two hours after its viewer left Jellyfin within seconds.

Jellyfin says what it plays (/Sessions) and what it records (/LiveTv/Timers), so whether
anybody is still behind a connection can be asked rather than guessed. The proxy's cleanup
loop calls `sweep`; one worker at a time, every SWEEP_SECONDS, it looks at every connection
from a Jellyfin server whose "close channels nobody watches" switch is on, and closes the
ones that stayed unwatched for GRACE_SECONDS. Only that connection: anybody else on the
channel keeps watching, and the channel stops by itself, the stock way, when nobody is left.

Kept open, always:

- a channel Jellyfin plays to anybody, paused included, or records;
- everything of a server whose answer cannot be read, or that names a channel (playing or
  recording) that none of its connections is on by name, or a live session without a
  channel: the names are written by different hands (see media_servers._same_channel), and a
  session that cannot be placed may be the one on this channel;
- a connection younger than GRACE_SECONDS, or seen unwatched for less than that in a row:
  Jellyfin registers a session a moment after it opens the stream, and the list is read
  only every SWEEP_SECONDS;
- Plex: it has no known way to say what it is recording, and losing a recording is worse
  than a channel left open (see media_servers.recording_channels).

A connection is Jellyfin's when it comes from that server's address and calls itself Jellyfin
("Jellyfin-Server/...") or ffmpeg ("Lavf/...", what Jellyfin's ffmpeg sends when it pulls the
tuner itself); when another configured server shares the address, only when it says Jellyfin.
A player on the same machine, Dispatcharr's own recordings and Dispatch More's helpers are
never one of them.

Off (the switch on each Jellyfin server, off by default): one read of the media server list
every SWEEP_SECONDS by one worker, and nothing else.
"""

import logging
import time

import gevent

from . import media_servers

logger = logging.getLogger("live_proxy.media_servers")

SWEEP_SECONDS = 30
SWEEP_KEY = "live:left_open:sweep"
GRACE_SECONDS = 180
# {"<channel uuid>|<client id>": when it was first seen with nobody behind it}
UNWATCHED_KEY = "live:left_open:unwatched"


def _s(value):
    return value.decode() if isinstance(value, bytes) else value


def sweep(redis_client):
    """Called from the cleanup loop. Returns at once; the look runs in a greenlet."""
    if not redis_client:
        return
    try:
        if not redis_client.set(SWEEP_KEY, "1", nx=True, ex=SWEEP_SECONDS):
            return
        if not _servers_on(media_servers.load_servers()):
            return
        gevent.spawn(_sweep_safely, redis_client)
    except Exception as e:
        logger.debug(f"Could not look for channels left open: {e}")


def _servers_on(servers):
    return [
        server for server in servers
        if media_servers.is_enabled(server)
        and media_servers.kind(server) == "jellyfin"
        and server.get("close_when_left")
    ]


def _sweep_safely(redis_client):
    from django.db import close_old_connections

    try:
        look(redis_client)
    except Exception as e:
        logger.warning(f"Looking for channels left open failed: {e}")
    finally:
        try:
            close_old_connections()
        except Exception:
            pass


def connections(redis_client):
    """Every viewer of every open channel: (channel uuid, client id, user agent, ip, connected at)."""
    found = []
    for key in redis_client.scan_iter("live:channel:*:clients"):
        key = _s(key)
        parts = key.split(":")
        if len(parts) != 4:
            continue
        channel_uuid = parts[2]
        for client_id in redis_client.smembers(key) or ():
            client_id = _s(client_id)
            data = {
                _s(k): _s(v)
                for k, v in (redis_client.hgetall(f"{key}:{client_id}") or {}).items()
            }
            try:
                connected_at = float(data.get("connected_at") or 0)
            except ValueError:
                connected_at = 0.0
            found.append((channel_uuid, client_id, data.get("user_agent") or "",
                          (data.get("ip_address") or "").lower(), connected_at))
    return found


def _jellyfin_agent(user_agent, shared_address) -> bool:
    """Whether a connection from the server's address says it is Jellyfin (see the module)."""
    from apps.channels.captions import is_caption_client
    from .probation import is_recording

    agent = (user_agent or "").lower()
    if is_recording(user_agent) or is_caption_client(user_agent):
        return False
    if "jellyfin" in agent:
        return True
    return agent.startswith("lavf") and not shared_address


def _names(channel_uuids):
    """Each channel's names: its own and the one the tuner lineup gives (0.33's effective name)."""
    from apps.channels.managers import with_effective_values
    from apps.channels.models import Channel

    names = {}
    for channel in with_effective_values(Channel.objects.filter(uuid__in=list(channel_uuids))):
        names[str(channel.uuid)] = {
            name for name in (channel.name, getattr(channel, "effective_name", None)) if name
        }
    return names


def _on(reported, names):
    return any(media_servers._same_channel(reported, name) for name in names)


def unwatched(server, theirs, names):
    """
    The channels among `theirs` (this server's connections) that nobody watches or records
    through it, or None when that cannot be told -- which keeps every one of them open.
    """
    raw = media_servers._get(server, "/Sessions")
    if not isinstance(raw, list):
        return None
    recording = media_servers.jellyfin_recording(media_servers._get(server, "/LiveTv/Timers"))
    if recording is None:
        return None

    reported = [
        session.get("channel") or ""
        for session in media_servers.jellyfin_playing(server, raw)
        if session.get("live")
    ] + sorted(recording)
    channels = {channel_uuid for channel_uuid, *_ in theirs}
    held = set()
    for name in reported:
        on = {uuid for uuid in channels if _on(name, names.get(uuid, ()))}
        if not on:
            # Playing or recording something no connection is on by name: it may be one of
            # them under a name written differently, so nothing of this server is closed
            return None
        held |= on
    return channels - held


def look(redis_client, now=None):
    """One look at every connection of the servers that are switched on (see the module)."""
    now = time.time() if now is None else now
    servers = media_servers.load_servers()
    on = _servers_on(servers)
    seen = {_s(k): float(_s(v)) for k, v in (redis_client.hgetall(UNWATCHED_KEY) or {}).items()}
    still = {}
    closed = []
    everyone = connections(redis_client) if on else []
    names = _names({c[0] for c in everyone}) if everyone else {}
    for server in on:
        mine = media_servers.addresses(server)
        shared = set()
        for other in servers:
            if other is not server and media_servers.is_enabled(other):
                shared |= media_servers.addresses(other)
        theirs = [c for c in everyone if c[3] in mine and _jellyfin_agent(c[2], c[3] in shared)]
        if not theirs:
            continue
        nobody = unwatched(server, theirs, names)
        if not nobody:
            continue
        for channel_uuid, client_id, _agent, _ip, connected_at in theirs:
            if channel_uuid not in nobody:
                continue
            key = f"{channel_uuid}|{client_id}"
            since = seen.get(key, now)
            if now - since >= GRACE_SECONDS and connected_at and now - connected_at >= GRACE_SECONDS:
                closed.append((server, channel_uuid, client_id, now - since))
            else:
                still[key] = since

    pipe = redis_client.pipeline()
    pipe.delete(UNWATCHED_KEY)
    if still:
        pipe.hset(UNWATCHED_KEY, mapping={k: str(v) for k, v in still.items()})
        pipe.expire(UNWATCHED_KEY, GRACE_SECONDS * 4)
    pipe.execute()

    for server, channel_uuid, client_id, for_seconds in closed:
        _close(redis_client, server, channel_uuid, client_id, for_seconds)
    return closed


def _close(redis_client, server, channel_uuid, client_id, for_seconds):
    from . import health
    from .services.channel_service import ChannelService

    name = server.get("name") or "Jellyfin"
    logger.info(
        f"{name} kept {channel_uuid} open with nobody watching or recording for "
        f"{int(for_seconds)}s; closing its connection {client_id}"
    )
    try:
        ChannelService.stop_client(channel_uuid, client_id)
    except Exception as e:
        logger.warning(f"Could not close {name}'s connection {client_id} on {channel_uuid}: {e}")
        return
    health.record_event(
        redis_client, channel_uuid, "media server left open",
        f"{name} played this channel for nobody for {int(for_seconds // 60)} min; its "
        f"connection was closed",
    )
