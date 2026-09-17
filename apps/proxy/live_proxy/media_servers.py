"""Media servers (Plex and Jellyfin), and what they can tell us about a channel start.

Dispatcharr can see how long it took to hand the first video to a media server, but not what
the server did with it afterwards. Plex can: every playing stream says whether it is being
transcoded, how fast, and whether the player is still buffering. That last one is the gap a
viewer actually feels, so after a channel start on a media server we watch its sessions for a
short while and add what we find to the start on the Diagnostics page.

Nothing here is on the path of a stream: it runs in the background, everything is wrapped, and
a server that is unreachable simply adds nothing. The token is stored like an M3U password:
write-only in the API, never sent back to the browser.
"""

import json
import logging
import secrets
import socket
import time
from urllib.parse import quote, unquote, urlparse

import gevent
import requests

logger = logging.getLogger("live_proxy")

SETTINGS_KEY = "media-servers"
REQUEST_TIMEOUT = 5
# Making a DVR makes the server scan the tuner and load a guide before it answers, which takes
# much longer than reading something from it.
SLOW_TIMEOUT = 60

# How long, and how often, sessions are watched after a channel start. Plex needs a moment to
# create the session, and the buffering we are measuring is a handful of seconds.
WATCH_SECONDS = 25
WATCH_INTERVAL = 0.5
# What is playing on the media servers right now, kept for the moment it takes a viewer to
# switch channels. Refreshed in the background (see refresh_sessions), never in a request.
SESSIONS_KEY = "live:media_servers:sessions"
SESSIONS_TTL = 20
SESSIONS_REFRESH = 2.0
SESSIONS_REFRESH_KEY = "live:media_servers:sessions_refreshed"

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


def kind(server) -> str:
    """Which kind of media server this is. Plex unless it says otherwise."""
    return (server.get("kind") or "plex").lower()


def _headers(server):
    """How each kind of server wants its token."""
    token = server.get("token") or ""
    if kind(server) == "jellyfin":
        # Both are accepted; the header is the older one and the simplest to get right
        return {
            "Accept": "application/json",
            "X-Emby-Token": token,
            "Authorization": f'MediaBrowser Token="{token}"',
        }
    return {"Accept": "application/json", "X-Plex-Token": token}


def _get(server, path, params=None):
    """One read from a media server. Returns the parsed body, or None when it cannot be read."""
    url = f"{clean_url(server.get('url'))}{path}"
    try:
        response = requests.get(
            url,
            params=params or {},
            headers=_headers(server),
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
    if kind(server) == "jellyfin":
        return _check_jellyfin(server)
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


def _check_jellyfin(server):
    """/System/Info needs the key, so it answers both questions at once."""
    try:
        response = requests.get(
            f"{clean_url(server.get('url'))}/System/Info",
            headers=_headers(server),
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": f"Could not reach the server: {e.__class__.__name__}"}
    if response.status_code in (401, 403):
        return {"ok": False, "error": "The server did not accept this API key"}
    if not response.ok:
        return {"ok": False, "error": f"The server answered with HTTP {response.status_code}"}
    info = response.json() or {}
    return {
        "ok": True,
        "name": info.get("ServerName") or "Jellyfin",
        "version": info.get("Version", ""),
    }


def _jellyfin_sessions(server):
    """
    What Jellyfin is playing, in the same shape as Plex's.

    A live channel is an item of type TvChannel, which is what the overlap cares about; a
    film someone is watching is not a tuner and is left out.
    """
    playing = []
    for session in _get(server, "/Sessions") or ():
        item = session.get("NowPlayingItem") or {}
        if not item:
            continue
        transcoding = session.get("TranscodingInfo") or {}
        play_state = session.get("PlayState") or {}
        playing.append({
            "title": item.get("Name", ""),
            "user": session.get("UserName", ""),
            "player": session.get("DeviceName") or session.get("Client") or "",
            "device_id": session.get("DeviceId", ""),
            "state": "paused" if play_state.get("IsPaused") else "playing",
            # Jellyfin says when the session was last active, not when it started
            "started_at": _jellyfin_time(session.get("LastActivityDate")),
            "live": item.get("Type") == "TvChannel",
            "watching": _what(item.get("Type")),
            "decision": _jellyfin_decision(transcoding),
            "speed": 0.0,
            "transcoding": bool(transcoding),
            "ready": 0.0,
            # PositionTicks are 100 nanoseconds each; only its movement is used
            "position": float(play_state.get("PositionTicks") or 0) / 10_000_000,
            "server": server.get("name") or "Jellyfin",
        })
    return playing


def _jellyfin_time(value):
    """A time Jellyfin sent, as seconds, or 0 when it cannot be read."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _jellyfin_decision(transcoding) -> str:
    """
    What the server is doing to the stream, from Jellyfin's two "is it direct" flags.

    They are read as "is exactly False", not as "is falsy": a missing flag means Jellyfin did
    not say, which is not the same as saying it is transcoding, and treating it as transcoding
    would make every session look expensive.
    """
    if not transcoding:
        return "direct play"
    parts = [
        kind_of
        for kind_of, field in (("video", "IsVideoDirect"), ("audio", "IsAudioDirect"))
        if transcoding.get(field) is False
    ]
    return f"transcode ({' + '.join(parts)})" if parts else "direct play"


def sessions(server):
    """
    What is playing right now, in one shape whatever the server is.

    This is the contract the rest of the file depends on: the page shows it, _watch() follows
    it through a start, and sole_device()/switching_device() decide who a request belongs to
    from it. Two fields are worth knowing about:

    - "live" is true only for a live channel, which is the only thing that comes through
      Dispatcharr; a film on the same server is a session but not our business;
    - "position" is where the player is in the stream, and is None when the server does not
      say (Plex does not, for live TV). None means "cannot tell", not "at the beginning", and
      _watch() treats the two differently.
    """
    if kind(server) == "jellyfin":
        return _jellyfin_sessions(server)
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
            "watching": "live TV" if item.get("live") == "1" else _what(item.get("type")),
            "decision": _decision(transcode),
            "speed": float(transcode.get("speed") or 0) if transcode else 0.0,
            "transcoding": bool(transcode),
            # How much video the server has ready: while this is 0 it has produced nothing yet
            "ready": float(transcode.get("maxOffsetAvailable") or 0) if transcode else 0.0,
            # Where the player is in the stream. When this moves, video is really being shown.
            # Live sessions do not have it at all, so it is None there rather than 0.
            "position": float(item["viewOffset"]) if "viewOffset" in item else None,
            "server": server.get("name") or "Plex",
        })
    return playing


def _what(item_type) -> str:
    """What someone is watching, in words: only live TV comes through Dispatcharr."""
    return {
        "TvChannel": "live TV",
        "Movie": "a film",
        "movie": "a film",
        "Episode": "an episode",
        "episode": "an episode",
        "Audio": "music",
        "track": "music",
    }.get(item_type or "", item_type or "something else")


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


def _post(server, path, params=None, json_body=None, timeout=REQUEST_TIMEOUT):
    """One change on a media server. Returns True when it was accepted."""
    url = f"{clean_url(server.get('url'))}{path}"
    try:
        response = requests.post(
            url,
            params=params or {},
            json=json_body,
            headers=_headers(server),
            timeout=timeout,
        )
        response.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"Media server refused {path}: {e}")
        return False


def _delete(server, path, params=None):
    url = f"{clean_url(server.get('url'))}{path}"
    try:
        response = requests.delete(
            url,
            params=params or {},
            headers=_headers(server),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"Media server refused to delete {path}: {e}")
        return False


def _live_tv_config(server):
    """Jellyfin keeps its tuners and guides in one settings blob."""
    return _get(server, "/System/Configuration/livetv") or {}


def _jellyfin_tuners(server, our_hosts=()):
    config = _live_tv_config(server)
    # One guide covers every tuner here too, so each tuner shows the same address
    guide = next(
        (
            provider.get("Path") or ""
            for provider in config.get("ListingProviders") or ()
            if provider.get("Path")
        ),
        "",
    )
    tuners = []
    for host in config.get("TunerHosts") or ():
        url = host.get("Url", "")
        host_name = (urlparse(url).hostname or "").lower()
        tuners.append({
            "id": str(host.get("Id") or ""),
            "uuid": str(host.get("Id") or ""),
            "title": host.get("FriendlyName") or host.get("Type") or "tuner",
            "uri": url,
            "model": host.get("Type", ""),
            # Jellyfin does not say whether a tuner answered; it either works or it does not
            "state": "",
            "tuners": int(host.get("TunerCount") or 0),
            # A guide covers all tuners, so a Jellyfin tuner is never "in no DVR"
            "dvr_id": "guide",
            "guide": guide,
            "ours": host_name in set(our_hosts) if host_name else False,
        })
    return tuners


def _jellyfin_guides(server):
    """Jellyfin has guides instead of DVRs: a source of programmes, covering the tuners."""
    return [
        {
            "id": str(provider.get("Id") or ""),
            "title": provider.get("Path") or provider.get("Type") or "guide",
            "tuners": ["all tuners"] if provider.get("EnableAllTuners") else [],
            "lineups": [provider.get("Type", "")],
            "guide": provider.get("Path") or "",
            "devices": [],
        }
        for provider in _live_tv_config(server).get("ListingProviders") or ()
    ]


def dvrs(server):
    """The DVRs on the server, with the devices that belong to each."""
    container = (_get(server, "/livetv/dvrs") or {}).get("MediaContainer") or {}
    return container.get("Dvr") or container.get("DVR") or []


def dvr_list(server):
    """The DVRs as the page offers them: which one to put a new tuner in, and what is in it."""
    if kind(server) == "jellyfin":
        return _jellyfin_guides(server)
    return [
        {
            "id": str(dvr.get("key")),
            "title": dvr.get("lineupTitle") or dvr.get("language") or f"DVR {dvr.get('key')}",
            "tuners": [device.get("title") or "tuner" for device in dvr.get("Device") or ()],
            "lineups": [lineup.get("title") or "" for lineup in dvr.get("Lineup") or ()],
            # The one guide this DVR uses, so the page can show it against each of its tuners
            "guide": guide_url(dvr.get("lineup")),
            "devices": [device.get("uuid", "") for device in dvr.get("Device") or ()],
        }
        for dvr in dvrs(server)
    ]


def set_guide(server, dvr_id, xmltv_url, title, device_uuids=()):
    """
    Change the guide a DVR uses.

    A DVR is stored with its guide and its tuners together, so both go back or the server
    takes the change as a DVR with no tuners left in it. There is no endpoint that changes
    the guide on its own.
    """
    if kind(server) == "jellyfin":
        # A Jellyfin guide is its own object: replace it rather than editing a DVR
        return add_guide(server, xmltv_url, title)
    params = {
        "lineup": xmltv_lineup(xmltv_url, title),
        "language": "eng",
    }
    if device_uuids:
        # Repeated, one per tuner: this is a list of devices, not one joined value
        params["device"] = list(device_uuids)
    return _put(server, f"/livetv/dvrs/{dvr_id}", params, timeout=SLOW_TIMEOUT)


def delete_dvr(server, dvr_id):
    """Remove a DVR (a guide on Jellyfin). Its tuners stay registered on the server."""
    if kind(server) == "jellyfin":
        return delete_guide(server, dvr_id)
    return _delete(server, f"/livetv/dvrs/{dvr_id}")


def _put(server, path, params=None, timeout=None):
    url = f"{clean_url(server.get('url'))}{path}"
    try:
        response = requests.put(
            url,
            params=params or {},
            headers=_headers(server),
            timeout=timeout or REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"Media server refused {path}: {e}")
        return False


def xmltv_lineup(xmltv_url, title) -> str:
    """The lineup a media server stores for an XMLTV guide: the address, escaped, then a #title."""
    return f"lineup://tv.plex.providers.epg.xmltv/{quote(xmltv_url, safe='')}#{title}"


def add_lineup(server, dvr_id, xmltv_url, title):
    """
    Give a DVR another guide, for a channel source being added to it.

    A DVR holds a lineup per channel source, so a tuner put into a DVR that was already
    there needs its guide added as well or its channels are listed against nothing. This is
    what the server's own settings do, in this order: the guide first, then the tuner.
    """
    if kind(server) == "jellyfin":
        # Its guide already covers every tuner
        return True
    return _put(
        server, f"/livetv/dvrs/{dvr_id}/lineups", {"lineup": xmltv_lineup(xmltv_url, title)}
    )


def name_device(server, device_id, title):
    """
    Give a tuner its name and switch it on.

    A tuner added through the API has no title until this is done: the server shows it as a
    blank row, and a tuner it does not consider enabled is not used. The server's own
    settings do this straight after adding one.
    """
    if kind(server) == "jellyfin":
        # Its name is given when it is added
        return True
    return _put(server, f"/media/grabbers/devices/{device_id}", {
        "title": title,
        "enabled": 1,
    })


def _profile_in(address, marker) -> str:
    """The channel profile named in one of our addresses, after "hdhr" or after "epg"."""
    parts = [part for part in str(address).split("?", 1)[0].split("/") if part]
    if marker not in parts:
        return ""
    index = parts.index(marker) + 1
    return unquote(parts[index]).lower() if len(parts) > index else ""


def _guide_for(uri, lineups, fallback) -> str:
    """
    Which of a DVR's guides belongs to this tuner.

    A DVR holds a guide per channel source, but it does not say which is whose: the tuners
    carry no lineup of their own and the guides carry no tuner. Both of ours name the same
    channel profile in their address, so they are matched on that. A tuner that is not ours,
    or one whose guide is not, falls back to the guide the DVR was made with.
    """
    profile = _profile_in(uri, "hdhr")
    if profile:
        for lineup in lineups:
            if _profile_in(lineup, "epg") == profile:
                return lineup
    return fallback


def set_tuner_uri(server, device_id, uri, title=None) -> bool:
    """
    Point a tuner that is already registered at another address.

    Asking is not enough: a server may take the request and keep the address it had, so
    whether it changed is read back rather than assumed. Says whether it really moved.
    """
    if kind(server) == "jellyfin":
        # Saving a tuner host with the Id it already has replaces that one
        _post(server, "/LiveTv/TunerHosts", json_body={
            "Id": str(device_id),
            "Type": "hdhomerun",
            "Url": uri,
            "FriendlyName": title or "Dispatcharr",
            "AllowHWTranscoding": True,
            "EnableStreamLooping": False,
        })
    else:
        params = {"uri": uri}
        if title:
            params["title"] = title
        _put(server, f"/media/grabbers/devices/{device_id}", params)

    return any(
        str(tuner["id"]) == str(device_id) and tuner["uri"] == uri
        for tuner in tuners(server)
    )


def guide_url(lineup) -> str:
    """
    The plain address of the guide a DVR uses, back out of the lineup it stores.

    Worth showing on the page: a DVR whose guide covers other channels than its tuners serve
    looks like a working DVR with no programmes against half of it, and the address is the
    only place that is visible.
    """
    if not lineup:
        return ""
    address = str(lineup).split("/", 3)[-1] if "//" in str(lineup) else str(lineup)
    return unquote(address.split("#", 1)[0])


def create_dvr(server, device_uuid, xmltv_url, title, language="eng"):
    """
    Make a DVR for this tuner, with Dispatcharr's own EPG as its guide.

    The lineup is the format the server stores for an XMLTV guide: the scheme, the address of
    the guide with everything escaped, and the title after a #. Nothing else about the DVR is
    set, so the server's own defaults apply.
    """
    if kind(server) == "jellyfin":
        # Jellyfin has guides instead: one source of programmes, covering every tuner
        return add_guide(server, xmltv_url, title)
    return _post(
        server,
        "/livetv/dvrs",
        {
            "device": device_uuid,
            "lineup": xmltv_lineup(xmltv_url, title),
            "language": language or "eng",
        },
        timeout=SLOW_TIMEOUT,
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

    Jellyfin has no DVRs: a guide covers every tuner, so there is nothing to attach.
    """
    if kind(server) == "jellyfin":
        return True
    return _put(server, f"/livetv/dvrs/{dvr_id}/devices/{device_id}")


def tuners(server, our_hosts=()):
    """
    Every tuner device the server knows, with what is worth acting on: whether it answers,
    whether it belongs to a DVR (a tuner outside one does nothing), and whether it is ours.
    """
    if kind(server) == "jellyfin":
        return _jellyfin_tuners(server, our_hosts)
    container = (_get(server, "/media/grabbers/devices") or {}).get("MediaContainer") or {}
    # A DVR holds a guide per channel source, so the guide shown against a tuner is the one
    # for its own channels rather than whichever the DVR happens to have been made with.
    in_dvr = {}
    dvr_guides = {}
    dvr_lineups = {}
    for dvr in dvrs(server):
        key = str(dvr.get("key"))
        dvr_guides[key] = guide_url(dvr.get("lineup"))
        dvr_lineups[key] = [
            guide_url(lineup.get("id"))
            for lineup in dvr.get("Lineup") or ()
            if lineup.get("id")
        ]
        for device in dvr.get("Device") or ():
            in_dvr[str(device.get("key"))] = key

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
            "guide": _guide_for(
                uri,
                dvr_lineups.get(in_dvr.get(key, ""), ()),
                dvr_guides.get(in_dvr.get(key, ""), ""),
            ),
            "ours": host in set(our_hosts) if host else False,
        })
    return found


def add_tuner(server, uri, name=None, tuner_count=None, tuner_type="hdhomerun"):
    """
    Register Dispatcharr as a tuner.

    Jellyfin can take it as an HDHomeRun or as an M3U playlist; Plex only knows HDHomeRun, so
    the kind is ignored there.
    """
    if kind(server) == "jellyfin":
        body = {
            "Type": "m3u" if tuner_type == "m3u" else "hdhomerun",
            "Url": uri,
            "FriendlyName": name or "Dispatcharr",
            "AllowHWTranscoding": True,
            "EnableStreamLooping": False,
        }
        if tuner_count:
            body["TunerCount"] = int(tuner_count)
        return _post(server, "/LiveTv/TunerHosts", json_body=body)
    return _post(server, "/media/grabbers/devices", {"uri": uri})


def delete_tuner(server, device_id):
    if kind(server) == "jellyfin":
        return _delete(server, "/LiveTv/TunerHosts", {"id": device_id})
    return _delete(server, f"/media/grabbers/devices/{device_id}")


def delete_guide(server, guide_id):
    """Jellyfin: remove a guide. Its tuners keep working, with no programmes."""
    return _delete(server, "/LiveTv/ListingProviders", {"id": guide_id})


def add_guide(server, xmltv_url, name=None):
    """Jellyfin: add Dispatcharr's EPG as a guide, covering every tuner."""
    return _post(
        server,
        "/LiveTv/ListingProviders",
        {"validateListings": "false", "validateLogin": "false"},
        json_body={
            "Type": "xmltv",
            "Path": xmltv_url,
            "EnableAllTuners": True,
            "UserAgent": name or "Dispatcharr",
        },
    )


def refresh_guide(server):
    """Jellyfin: run the guide refresh, which is a scheduled task rather than an endpoint."""
    for task in _get(server, "/ScheduledTasks") or ():
        if (task.get("Key") or "").lower() == "refreshguide":
            return _post(server, f"/ScheduledTasks/Running/{task.get('Id')}")
    logger.debug("Jellyfin has no guide refresh task")
    return False


def device_channels(server, device_id):
    """The channels a tuner found when it was scanned."""
    container = (
        _get(server, f"/media/grabbers/devices/{device_id}/channels") or {}
    ).get("MediaContainer") or {}
    return [
        channel.get("identifier")
        for channel in container.get("DeviceChannel") or ()
        if channel.get("identifier")
    ]


def enable_channels(server, device_id, channels=None):
    """
    Switch a tuner's channels on, and map each to the guide channel with the same number.

    A scan only finds channels; until they are enabled the media server shows the tuner with
    "0 enabled" and none of them appear, which looks like the tuner is not working at all.
    Dispatcharr's own EPG uses the same numbers on both sides, so each channel maps to itself.
    """
    if kind(server) == "jellyfin":
        # Jellyfin enables every channel a tuner reports; there is no map to set
        return True
    channels = channels or device_channels(server, device_id)
    if not channels:
        return False
    # One comma separated value, not the parameter repeated: given it twice the server keeps
    # the last one and enables a single channel.
    params = {"channelsEnabled": ",".join(str(channel) for channel in channels)}
    for channel in channels:
        # Both maps, which is what the server's own settings send. Dispatcharr's EPG uses
        # the same numbers on both sides, so each channel maps to itself.
        params[f"channelMappingByKey[{channel}]"] = channel
        params[f"channelMapping[{channel}]"] = channel
    return _put(server, f"/media/grabbers/devices/{device_id}/channelmap", params)


def sync_tuner(server, device_id, dvr_id=None):
    """
    Rescan the tuner's channels, switch them on, and reload the guide of its DVR.

    The scan is what finds channels, enabling them is what makes them appear, and the guide
    reload is what puts programmes against them: all three, or the tuner looks broken.
    """
    if kind(server) == "jellyfin":
        # Jellyfin rescans its tuners while refreshing the guide, and has no channel map
        return refresh_guide(server)
    scanned = _post(server, f"/media/grabbers/devices/{device_id}/scan")
    enabled = enable_channels(server, device_id)
    reloaded = _post(server, f"/livetv/dvrs/{dvr_id}/reloadGuide") if dvr_id else False
    return scanned or enabled or reloaded


def refresh_sessions(redis_client):
    """
    Keep a fresh list of what the media servers are playing, so a stream request never has to
    wait for one. Called from the proxy's cleanup loop; does nothing most times it is called.
    """
    if not redis_client:
        return
    try:
        if not redis_client.set(SESSIONS_REFRESH_KEY, "1", nx=True, ex=int(SESSIONS_REFRESH)):
            return
        servers = [server for server in load_servers() if is_enabled(server)]
        if not servers:
            return
        playing = []
        for server in servers:
            playing.extend(sessions(server))
        redis_client.setex(SESSIONS_KEY, SESSIONS_TTL, json.dumps(playing))
    except Exception as e:
        logger.debug(f"Could not refresh the media server sessions: {e}")


def cached_sessions(redis_client):
    """What the media servers were playing a moment ago (see refresh_sessions)."""
    try:
        raw = redis_client.get(SESSIONS_KEY)
        return json.loads(raw) if raw else []
    except Exception:
        return []


# Which of a media server's devices is watching which channel, learned a moment after the
# channel started (see _watch). Outlives the channel a little, so a device that has just
# stopped watching can still be recognised as the one switching.
CHANNEL_DEVICE_KEY = "live:media_servers:channel:{channel_uuid}"
CHANNEL_DEVICE_TTL = 4 * 3600


def bind_device(redis_client, channel_uuid, session):
    """
    Remember which device is watching this channel, and tell the channel's clients, so the
    people watching through a media server are as recognisable as any other player.
    """
    from .redis_keys import RedisKeys

    device = f"server|{session.get('device_id') or ''}"
    if not session.get("device_id") or not channel_uuid:
        return
    try:
        redis_client.setex(
            CHANNEL_DEVICE_KEY.format(channel_uuid=channel_uuid), CHANNEL_DEVICE_TTL, device
        )
        remember_device_name(redis_client, device, session)
        for client_id in redis_client.smembers(RedisKeys.clients(channel_uuid)) or ():
            client_id = client_id.decode() if isinstance(client_id, bytes) else client_id
            redis_client.hset(
                RedisKeys.client_metadata(channel_uuid, client_id), "server_device", device
            )
        logger.info(
            f"Media server: {session.get('user') or 'someone'} on "
            f"{session.get('player') or 'a device'} is watching channel {channel_uuid}"
        )
    except Exception as e:
        logger.debug(f"Could not remember who is watching channel {channel_uuid}: {e}")


DEVICE_NAME_KEY = "live:media_servers:device:{device}"


def remember_device_name(redis_client, device, session):
    """
    What to call a device on the Diagnostics page: who is watching, on what, through which
    server. With more than one media server, "Chris · Shield" alone says too little.
    """
    who = " · ".join(part for part in (session.get("user"), session.get("player")) if part)
    name = f"{who} ({session['server']})" if who and session.get("server") else who
    if name:
        redis_client.setex(
            DEVICE_NAME_KEY.format(device=device), CHANNEL_DEVICE_TTL, name
        )


def device_name(redis_client, device):
    """The readable name of a media server device, when one was seen (see bind_device)."""
    try:
        return _as_str(redis_client.get(DEVICE_NAME_KEY.format(device=device)))
    except Exception:
        return None


def _as_str(value):
    return value.decode() if isinstance(value, bytes) else value


def switching_device(redis_client):
    """
    The device that was watching a channel which is no longer running: the one that is
    switching. None when it is not clear, which is when more than one has just stopped.
    """
    from .redis_keys import RedisKeys

    try:
        watching = {
            session.get("device_id")
            for session in cached_sessions(redis_client)
            if session.get("live")
        }
        stopped = set()
        for key in redis_client.scan_iter(CHANNEL_DEVICE_KEY.format(channel_uuid="*")) or ():
            key = _as_str(key)
            channel_uuid = key[len("live:media_servers:channel:"):]
            if redis_client.exists(RedisKeys.channel_metadata(channel_uuid)):
                continue
            device = _as_str(redis_client.get(key))
            # It is only switching if its server still says it is watching something
            if device and device.split("|", 1)[-1] in watching:
                stopped.add(device)
        return next(iter(stopped)) if len(stopped) == 1 else None
    except Exception as e:
        logger.debug(f"Could not work out which device is switching: {e}")
        return None


def sole_device(redis_client):
    """
    The one device streaming from a media server right now, or None when there are several.

    A media server asks for a channel on behalf of whoever is watching, and the request says
    nothing about which of them it is. When only one of its devices is streaming, there is no
    doubt; with more than one there is, and a viewer that cannot be told apart takes no part
    in the overlap (which is what a media server did before this).
    """
    devices = {
        session.get("device_id")
        for session in cached_sessions(redis_client)
        if session.get("live") and session.get("device_id")
    }
    return next(iter(devices)) if len(devices) == 1 else None


def watch_start(redis_client, start_id, user_agent, started_at=None, ip=None, channel_uuid=None):
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
        gevent.spawn(_watch, redis_client, start_id, started_at or time.time(), channel_uuid)
    except Exception as e:
        logger.debug(f"Could not watch a media server for start {start_id}: {e}")


def _is_media_server(user_agent, ip=None) -> bool:
    from . import probation

    return probation.is_media_server(user_agent, ip)


def _watch(redis_client, start_id, started_at, channel_uuid=None):
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
            if "session opened" not in seen:
                # The first time the server names a session for this channel, its device
                # becomes the viewer of that channel (see bind_device)
                bind_device(redis_client, channel_uuid, session)
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
                    server_name=session.get("server", ""),
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
                server_name=session.get("server", ""),
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
