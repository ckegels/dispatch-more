"""Media servers (Plex for now), and what they can tell us about a channel start.

Dispatcharr can see how long it took to hand the first video to a media server, but not what
the server did with it afterwards. Plex can: every playing stream says whether it is being
transcoded, how fast, and whether the player is still buffering. That last one is the gap a
viewer actually feels, so after a channel start on a media server we watch its sessions for a
short while and add what we find to the start on the Diagnostics page.

Nothing here is on the path of a stream: it runs in the background, everything is wrapped, and
a server that is unreachable simply adds nothing. The token is stored like an M3U password:
write-only in the API, never sent back to the browser.
"""

import logging
import secrets
import socket
import time
from urllib.parse import quote, urlparse

import gevent
import requests

logger = logging.getLogger("live_proxy")

SETTINGS_KEY = "media-servers"
REQUEST_TIMEOUT = 5

# How long, and how often, sessions are watched after a channel start. Plex needs a moment to
# create the session, and the buffering we are measuring is a handful of seconds.
WATCH_SECONDS = 25
WATCH_INTERVAL = 0.5
# A session belongs to this start when it appeared after the channel was requested (a little
# before is allowed: the server's clock is not ours), and not too long after. Matching a
# session that was already running would measure someone else's stream.
MATCH_BEFORE = 5
MATCH_WINDOW = 15


HOSTS_CACHE_KEY = "live:media_servers:hosts"
HOSTS_CACHE_TTL = 60


def server_hosts() -> frozenset:
    """
    The addresses of the configured media servers, as hostnames and resolved IPs.

    A media server pulls a channel like any other client, and Plex does it with ffmpeg's
    User-Agent ("Lavf/..."), which says nothing about who is watching. Its address does: a
    request from a server we know is that server, whatever it calls itself.
    """
    from django.core.cache import cache

    try:
        cached = cache.get(HOSTS_CACHE_KEY)
    except Exception:
        cached = None
    if cached is not None:
        return frozenset(cached)

    hosts = set()
    for server in load_servers():
        host = urlparse(clean_url(server.get("url"))).hostname
        if not host:
            continue
        hosts.add(host.lower())
        try:
            for info in socket.getaddrinfo(host, None):
                hosts.add(info[4][0])
        except OSError:
            # A name that cannot be resolved right now still matches by name
            pass
    try:
        cache.set(HOSTS_CACHE_KEY, list(hosts), HOSTS_CACHE_TTL)
    except Exception:
        pass
    return frozenset(hosts)


def forget_hosts():
    from django.core.cache import cache

    try:
        cache.delete(HOSTS_CACHE_KEY)
    except Exception:
        pass


def load_servers():
    """The configured media servers, tokens included (for our own calls)."""
    from core.models import CoreSettings

    try:
        setting = CoreSettings.objects.filter(key=SETTINGS_KEY).first()
    except Exception as e:
        logger.debug(f"Could not read the media servers: {e}")
        return []
    value = getattr(setting, "value", None) or {}
    servers = value.get("servers") if isinstance(value, dict) else None
    return servers if isinstance(servers, list) else []


def save_servers(servers):
    from core.models import CoreSettings

    CoreSettings.objects.update_or_create(
        key=SETTINGS_KEY,
        defaults={"name": "Media Servers", "value": {"servers": servers}},
    )
    forget_hosts()


def public(server):
    """A server as the browser may see it: everything except the token."""
    return {
        "id": server.get("id"),
        "name": server.get("name") or "Plex",
        "kind": server.get("kind", "plex"),
        "url": server.get("url", ""),
        "has_token": bool(server.get("token")),
        "enabled": is_enabled(server),
    }


def is_enabled(server) -> bool:
    """Switched off means Dispatcharr stops talking to it; it stays configured."""
    return server.get("enabled", True) is not False


def new_id() -> str:
    return secrets.token_hex(4)


def clean_url(url) -> str:
    """The address without a trailing slash, so paths can simply be appended."""
    return str(url or "").strip().rstrip("/")


def _get(server, path, params=None):
    """One read from a media server. Returns the parsed body, or None when it cannot be read."""
    url = f"{clean_url(server.get('url'))}{path}"
    try:
        response = requests.get(
            url,
            params=params or {},
            headers={
                "Accept": "application/json",
                "X-Plex-Token": server.get("token") or "",
            },
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 401:
            logger.debug(f"Media server {url} refused the token")
            return None
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.debug(f"Could not read {url}: {e}")
        return None


def check(server):
    """
    Whether the server answers and the token works, and what it is.
    Returns {"ok": bool, "name": ..., "version": ..., "error": ...}.
    """
    url = f"{clean_url(server.get('url'))}/identity"
    try:
        response = requests.get(
            url,
            headers={"Accept": "application/json", "X-Plex-Token": server.get("token") or ""},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": f"Could not reach the server: {e.__class__.__name__}"}
    if response.status_code == 401:
        return {"ok": False, "error": "The server did not accept this token"}
    if not response.ok:
        return {"ok": False, "error": f"The server answered with HTTP {response.status_code}"}

    # /identity answers without a token too, so a token is only proven by a call that needs one
    container = (_get(server, "/status/sessions") or {}).get("MediaContainer")
    if container is None:
        return {"ok": False, "error": "The server did not accept this token"}
    identity = (response.json() or {}).get("MediaContainer", {})
    return {
        "ok": True,
        "name": identity.get("friendlyName") or "Plex",
        "version": identity.get("version", ""),
    }


def sessions(server):
    """What is playing right now, as the page and the start watcher need it."""
    container = (_get(server, "/status/sessions") or {}).get("MediaContainer") or {}
    playing = []
    for item in container.get("Metadata", []) or ():
        player = item.get("Player") or {}
        transcode = item.get("TranscodeSession") or {}
        playing.append({
            "title": item.get("title", ""),
            "user": (item.get("User") or {}).get("title", ""),
            "player": player.get("title") or player.get("product") or "",
            "device_id": player.get("machineIdentifier", ""),
            "state": player.get("state", ""),
            "started_at": float(item.get("addedAt") or 0),
            "live": item.get("live") == "1",
            "decision": _decision(transcode),
            "speed": float(transcode.get("speed") or 0) if transcode else 0.0,
            "transcoding": bool(transcode),
            # How much video the server has ready: while this is 0 it has produced nothing yet
            "ready": float(transcode.get("maxOffsetAvailable") or 0) if transcode else 0.0,
            # Where the player is in the stream. When this moves, video is really being shown.
            # Live sessions do not have it at all, so it is None there rather than 0.
            "position": float(item["viewOffset"]) if "viewOffset" in item else None,
        })
    return playing


def _decision(transcode) -> str:
    """Direct play, or what is being transcoded: the expensive thing a media server does."""
    if not transcode:
        return "direct play"
    parts = [
        kind
        for kind, field in (("video", "videoDecision"), ("audio", "audioDecision"))
        if transcode.get(field) == "transcode"
    ]
    return f"transcode ({' + '.join(parts)})" if parts else "direct play"


def _post(server, path, params=None):
    """One change on a media server. Returns True when it was accepted."""
    url = f"{clean_url(server.get('url'))}{path}"
    try:
        response = requests.post(
            url,
            params=params or {},
            headers={"Accept": "application/json", "X-Plex-Token": server.get("token") or ""},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"Media server refused {path}: {e}")
        return False


def _delete(server, path):
    url = f"{clean_url(server.get('url'))}{path}"
    try:
        response = requests.delete(
            url,
            headers={"Accept": "application/json", "X-Plex-Token": server.get("token") or ""},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"Media server refused to delete {path}: {e}")
        return False


def dvrs(server):
    """The DVRs on the server, with the devices that belong to each."""
    container = (_get(server, "/livetv/dvrs") or {}).get("MediaContainer") or {}
    return container.get("Dvr") or container.get("DVR") or []


def dvr_list(server):
    """The DVRs as the page offers them: which one to put a new tuner in."""
    return [
        {
            "id": str(dvr.get("key")),
            "title": dvr.get("lineupTitle") or dvr.get("language") or f"DVR {dvr.get('key')}",
        }
        for dvr in dvrs(server)
    ]


def _put(server, path):
    url = f"{clean_url(server.get('url'))}{path}"
    try:
        response = requests.put(
            url,
            headers={"Accept": "application/json", "X-Plex-Token": server.get("token") or ""},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"Media server refused {path}: {e}")
        return False


def create_dvr(server, device_uuid, xmltv_url, title, language="eng"):
    """
    Make a DVR for this tuner, with Dispatcharr's own EPG as its guide.

    The lineup is the format the server stores for an XMLTV guide: the scheme, the address of
    the guide with everything escaped, and the title after a #. Nothing else about the DVR is
    set, so the server's own defaults apply.
    """
    lineup = f"lineup://tv.plex.providers.epg.xmltv/{quote(xmltv_url, safe='')}#{title}"
    return _post(
        server,
        "/livetv/dvrs",
        {"device": device_uuid, "lineup": lineup, "language": language or "eng"},
    )


def dvr_for_device(server, device_id):
    """The DVR a tuner ended up in, once the server has made one."""
    for dvr in dvrs(server):
        for device in dvr.get("Device") or ():
            if str(device.get("key")) == str(device_id):
                return str(dvr.get("key"))
    return None


def attach_tuner(server, dvr_id, device_id):
    """
    Put a tuner into a DVR. A tuner that is in no DVR is registered but unused: the server
    does not scan it, does not put its channels in the guide, and cannot play from it.
    """
    return _put(server, f"/livetv/dvrs/{dvr_id}/devices/{device_id}")


def tuners(server, our_hosts=()):
    """
    Every tuner device the server knows, with what is worth acting on: whether it answers,
    whether it belongs to a DVR (a tuner outside one does nothing), and whether it is ours.
    """
    container = (_get(server, "/media/grabbers/devices") or {}).get("MediaContainer") or {}
    in_dvr = {}
    for dvr in dvrs(server):
        for device in dvr.get("Device") or ():
            in_dvr[str(device.get("key"))] = str(dvr.get("key"))

    found = []
    for device in container.get("Device") or ():
        uri = device.get("uri", "")
        host = (urlparse(uri).hostname or "").lower()
        key = str(device.get("key"))
        found.append({
            "id": key,
            "uuid": device.get("uuid", ""),
            "title": device.get("title") or device.get("model") or "tuner",
            "uri": uri,
            "model": device.get("model", ""),
            "state": device.get("status", ""),
            "tuners": int(device.get("tuners") or 0),
            "dvr_id": in_dvr.get(key, ""),
            "ours": host in set(our_hosts) if host else False,
        })
    return found


def add_tuner(server, uri):
    return _post(server, "/media/grabbers/devices", {"uri": uri})


def delete_tuner(server, device_id):
    return _delete(server, f"/media/grabbers/devices/{device_id}")


def sync_tuner(server, device_id, dvr_id=None):
    """Rescan the tuner's channels, and reload the guide of the DVR it belongs to."""
    scanned = _post(server, f"/media/grabbers/devices/{device_id}/scan")
    reloaded = _post(server, f"/livetv/dvrs/{dvr_id}/reloadGuide") if dvr_id else False
    return scanned or reloaded


def watch_start(redis_client, start_id, user_agent, started_at=None, ip=None):
    """
    After a channel start on a media server, watch its sessions for a moment and add what the
    server did to the start (see timing.finish). Returns at once; the watching runs in the
    background and never touches the stream.
    """
    try:
        if not start_id or not _is_media_server(user_agent, ip):
            return
        if not [server for server in load_servers() if is_enabled(server)]:
            return
        gevent.spawn(_watch, redis_client, start_id, started_at or time.time())
    except Exception as e:
        logger.debug(f"Could not watch a media server for start {start_id}: {e}")


def _is_media_server(user_agent, ip=None) -> bool:
    from . import probation

    return probation.is_media_server(user_agent, ip)


def _watch(redis_client, start_id, started_at):
    """Poll until the session is playing, or until it has been long enough."""
    from django.db import close_old_connections

    from . import timing

    try:
        servers = [server for server in load_servers() if is_enabled(server)]
        deadline = time.time() + WATCH_SECONDS
        # When each stage was first seen, measured from the moment the channel was requested
        seen = {}
        session = None
        position = None
        while time.time() < deadline:
            for server in servers:
                session = _session_for(server, started_at)
                if session:
                    break
            if not session:
                gevent.sleep(WATCH_INTERVAL)
                continue

            now = round(max(time.time() - started_at, 0), 1)
            seen.setdefault("session opened", now)
            if session["transcoding"]:
                seen.setdefault("transcode started", now)
            if session["ready"] > 0:
                seen.setdefault("first video ready", now)

            if session["state"] == "buffering":
                seen.setdefault("buffering", now)

            # A player that is further into the stream than a moment ago is really showing
            # video. Live sessions do not report a position, and then the server's own word
            # is all there is: it says "playing" before the first picture, so that number is
            # the earliest it could have been, not when the viewer saw something.
            if session["position"] is None:
                playing = session["state"] == "playing"
            else:
                playing = position is not None and session["position"] > position
                position = session["position"]

            if playing:
                seen.setdefault("playing", now)
                timing.update_start(
                    redis_client,
                    start_id,
                    server_user=session["user"],
                    server_player=session["player"],
                    # What the server thinks it is playing, so a wrong match can be spotted
                    server_title=session["title"],
                    server_decision=session["decision"],
                    server_speed=f"{session['speed']:.1f}" if session["speed"] else "",
                    # How long until the player actually played something
                    server_buffering=f"{seen['playing']:.1f}",
                    server_phases="|".join(f"{stage}={at}" for stage, at in seen.items()),
                    # Whether "playing" is the player's own position moving, or only the
                    # server saying so (all a live session offers)
                    server_playing_is_certain="1" if session["position"] is not None else "",
                )
                return
            gevent.sleep(WATCH_INTERVAL)
        if session:
            # It never started playing within the window: say how far it got
            timing.update_start(
                redis_client,
                start_id,
                server_user=session["user"],
                server_player=session["player"],
                server_title=session["title"],
                server_decision=session["decision"],
                server_phases="|".join(f"{stage}={at}" for stage, at in seen.items()),
                server_gave_up="1",
            )
        logger.debug(f"No media server session became playable for start {start_id}")
    except Exception as e:
        logger.debug(f"Could not follow a media server session for start {start_id}: {e}")
    finally:
        try:
            close_old_connections()
        except Exception:
            pass


def _session_for(server, started_at):
    """
    The live session this channel start belongs to: one that began when we handed the video
    over, not one that was already playing.

    The times come from two clocks, so a few seconds early is allowed. If they are further
    apart than that, nothing is matched and the start simply shows no server information,
    which is better than showing another stream's numbers.
    """
    for session in sessions(server):
        if not session["live"]:
            continue
        age = session["started_at"] - started_at
        if -MATCH_BEFORE <= age <= MATCH_WINDOW:
            return session
    return None
