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
            # What the server stops this session by, if asked to
            "session_id": str(session.get("Id") or ""),
            "user": session.get("UserName", ""),
            "player": session.get("DeviceName") or session.get("Client") or "",
            "device_id": session.get("DeviceId", ""),
            "state": "paused" if play_state.get("IsPaused") else "playing",
            # Jellyfin says when the session was last active, not when it started
            "started_at": _jellyfin_time(session.get("LastActivityDate")),
            # A live channel is usually an item of type TvChannel, but a session that came
            # through the guide is the programme, with the channel it is on beside it. Both
            # are live TV; taking only the first leaves those viewers out of the overlap.
            # Which channel this is, which is what says whether a session is the start we
            # are looking at. Started from the guide the item is the programme, with the
            # channel beside it; tuned directly, the item is the channel.
            "channel": item.get("ChannelName")
            or (item.get("Name", "") if item.get("Type") == "TvChannel" else ""),
            "live": item.get("Type") == "TvChannel" or bool(item.get("ChannelId")),
            "watching": (
                "live TV"
                if item.get("Type") == "TvChannel" or item.get("ChannelId")
                else _what(item.get("Type"))
            ),
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
            # What the server stops this session by, if asked to
            "session_id": str((item.get("Session") or {}).get("id") or ""),
            # Plex does not say which channel a live session is on. Measured on a real one:
            # the title is the programme, the type is what the programme is, and the fields
            # that would hold a channel are empty. What it is on is worked out from the
            # guide instead (see programme_now). Kept for the servers that do say.
            "channel": item.get("grandparentTitle") or item.get("parentTitle") or "",
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


def the_dvr(server):
    """
    The DVR a tuner belongs in, or nothing if the server has none yet.

    A server takes more DVRs through its API than its settings show: the extra ones are
    invisible and their tuners unusable. So there is no choosing between them; a tuner goes
    into the one that is there, and one is made only when there is none.
    """
    existing = dvr_list(server)
    return existing[0]["id"] if existing else ""


def dvr_list(server):
    """The DVRs as the page shows them: what is in each, and the guides they hold."""
    if kind(server) == "jellyfin":
        return _jellyfin_guides(server)
    return [
        {
            "id": str(dvr.get("key")),
            # Not lineupTitle: that is the name of whichever guide went in first, so the DVR
            # ends up called after one of its tuners, which says nothing about the DVR
            "title": "DVR",
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


# A server changing its tuners needs a moment between one change and the next. Measured the
# hard way: a Plex told to add a tuner, name it, give a DVR its guide and attach it, all as
# fast as they could be sent, and then asked to do it again eight seconds later, segfaulted.
MOVE_SETTLE = 2.0
MOVE_LOCK_KEY = "live:media_servers:moving"
MOVE_LOCK_SECONDS = 120


def _settle():
    """Let the server finish what it was just told to do before telling it the next thing."""
    gevent.sleep(MOVE_SETTLE)


def move_tuner(server, device, uri, guide_url=None, title=None, tuner_count=None,
               redis_client=None):
    """
    Point a registered tuner at another address by putting a new one there in its place.

    Plex has no way to change the address of a tuner it already has: it takes the request
    and keeps what it had (measured, not assumed -- the address is read back afterwards). So
    the change is made the only way it can be, which is the way its own settings do it: a
    tuner at the new address, named and switched on, its guide put in the DVR, attached
    where the old one was, and only then the old one removed.

    It is done slowly and one at a time, because a server doing this is fragile. Told to add
    a tuner, name it, add a guide and attach it as fast as the calls could be sent, and then
    told to do it again while it was still busy with the first, a Plex crashed outright and
    took its rollback with it -- leaving a tuner registered and in nothing. So each step
    waits, the new tuner is not scanned here at all (Sync does that, when you are ready),
    and a second move is refused while one is running.

    Returns (moved, what went wrong).
    """
    if redis_client is not None:
        if not redis_client.set(
            MOVE_LOCK_KEY, "1", nx=True, ex=MOVE_LOCK_SECONDS
        ):
            return False, (
                "Another tuner is being moved. Wait for that to finish: a server asked to "
                "change two at once is how one of them ends up in nothing."
            )
    try:
        return _move_tuner(server, device, uri, guide_url, title, tuner_count)
    finally:
        if redis_client is not None:
            try:
                redis_client.delete(MOVE_LOCK_KEY)
            except Exception:
                pass


def _move_tuner(server, device, uri, guide_url, title, tuner_count):
    was_in = device.get("dvr_id")
    name = title or device.get("title") or ""

    if not add_tuner(server, uri, name, tuner_count):
        return False, f"The server would not add a tuner at {uri}"
    _settle()

    replacement = next(
        (t for t in tuners(server) if t["uri"] == uri and t["id"] != device["id"]), None
    )
    if replacement is None:
        return False, "The server took the new tuner but did not list it afterwards"

    name_device(server, replacement["id"], name)
    _settle()

    if was_in:
        # Its guide first, then the tuner, which is the order the server's settings use
        if guide_url:
            add_lineup(server, was_in, guide_url, name)
            _settle()
        if not attach_tuner(server, was_in, replacement["id"]):
            # Put the server back as it was rather than leave a tuner in no DVR
            delete_tuner(server, replacement["id"])
            return False, "The server would not put the moved tuner back in its DVR"
        _settle()

    if not delete_tuner(server, device["id"]):
        # The new one works; the old one is still there and would be a second copy
        logger.warning(
            f"Moved the tuner to {uri} but could not remove the old one "
            f"({device.get('id')}); remove it by hand"
        )
        return True, "The tuner was moved, but the old one could not be removed"

    # Scanned last, and only after a longer wait than the steps before it. A tuner scanned
    # the moment it arrives answers 500: the server has taken it but is not ready to go
    # looking down it. By here everything else is done, so there is nothing left to lose if
    # the scan is refused -- the tuner is in its DVR either way and Sync can be pressed.
    if was_in:
        _settle()
        _settle()
        if not sync_tuner(server, replacement["id"], was_in):
            logger.info(
                f"Moved tuner {device.get('id')} to {uri}, but the scan was refused; "
                f"press Sync when the server has settled"
            )
            return True, (
                "Moved, but the server would not scan it yet. Press Sync in a moment."
            )

    logger.info(f"Moved tuner {device.get('id')} to {uri} as {replacement['id']}")
    return True, ""


def set_tuner_uri(server, device_id, uri, title=None, tuner_count=None) -> bool:
    """
    Point a tuner that is already registered at another address.

    Asking is not enough: a server may take the request and keep the address it had, so
    whether it changed is read back rather than assumed. Says whether it really moved.

    On a tuner of ours the number of tuners is part of the address, so changing how many
    connections it offers comes through here as well.
    """
    if kind(server) == "jellyfin":
        # Saving a tuner host with the Id it already has replaces that one. Its number of
        # tuners is a field here rather than part of the address.
        body = {
            "Id": str(device_id),
            "Type": "hdhomerun",
            "Url": uri,
            "FriendlyName": title or "Dispatcharr",
            "AllowHWTranscoding": True,
            "EnableStreamLooping": False,
        }
        if tuner_count:
            body["TunerCount"] = int(tuner_count)
        _post(server, "/LiveTv/TunerHosts", json_body=body)
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


RECORDING_KEY = "live:media_servers:recording"
RECORDING_TTL = 60


def recording_channels(server):
    """
    The channels this server is recording right now, by name.

    A recording is a channel nobody is watching, which is exactly what a channel somebody
    surfed past looks like. Without asking, the overlap can stop one: the recording pulls
    its stream while a viewer is streaming, so it is taken for that viewer's, and a switch
    a moment later frees "their" old channel. Losing a recording is the worst thing this
    fork could do, so the server is asked instead of guessed at.

    Jellyfin says, through its timers. Plex has no endpoint for this that is known to work,
    so its recordings are not seen and are protected only by the rules that apply to every
    channel: a recording running longer than the overlap window is never a candidate.
    """
    if kind(server) != "jellyfin":
        return set()
    names = set()
    try:
        for timer in _get(server, "/LiveTv/Timers") or ():
            if isinstance(timer, dict) and (timer.get("Status") or "") == "InProgress":
                if timer.get("ChannelName"):
                    names.add(timer["ChannelName"])
    except Exception as e:
        logger.debug(f"Could not ask {server.get('name')} what it is recording: {e}")
    return names


def channels_being_recorded(redis_client):
    """
    Every channel any media server is recording, cached because it is asked before a stop.

    Cached rather than asked each time: a stop is decided while a viewer waits, and a server
    that has gone away must not hold that up. An answer a minute old is good enough, because
    a recording lasts far longer than that.
    """
    if not redis_client:
        return set()
    try:
        cached = redis_client.get(RECORDING_KEY)
        if cached is not None:
            return set(json.loads(_as_str(cached)))
    except Exception:
        pass

    names = set()
    try:
        for server in load_servers():
            if is_enabled(server):
                names |= recording_channels(server)
        redis_client.setex(RECORDING_KEY, RECORDING_TTL, json.dumps(sorted(names)))
    except Exception as e:
        logger.debug(f"Could not work out what is being recorded: {e}")
    return names


def reload_guide(server, dvr_id):
    """
    Make a DVR read its guide again, after the guide it points at has changed.

    Without this the change is stored and nothing happens until the server next decides to
    look, so it appears not to have worked.
    """
    if kind(server) == "jellyfin":
        return refresh_guide(server)
    return _post(server, f"/livetv/dvrs/{dvr_id}/reloadGuide")


def stop_session(server, session_id):
    """
    Stop what a player is watching, from here rather than from the server's own screens.

    A player holding a slot is the thing worth doing something about, and hunting for it in
    the server's interface is the slow way. What the server does with the player afterwards
    is its own business: this asks it to stop, and says whether it agreed.
    """
    if kind(server) == "jellyfin":
        return _post(server, f"/Sessions/{session_id}/Playing/Stop")
    # Plex stops a session by its own id, with a reason it shows the person watching
    return (
        _get(
            server,
            "/status/sessions/terminate",
            {"sessionId": session_id, "reason": "Stopped from Dispatcharr"},
        )
        is not None
    )


def reload_every_guide():
    """
    Tell every media server to read its guide again, after Dispatcharr's own has changed.

    A server keeps its own copy and looks at ours on its own schedule, which is hours, so
    until it does it shows the programmes from before the refresh. Never raises: a guide
    that stays stale for a while is not worth failing an EPG refresh over.
    """
    reloaded = 0
    for server in load_servers():
        if not is_enabled(server):
            continue
        try:
            if kind(server) == "jellyfin":
                reloaded += 1 if refresh_guide(server) else 0
            else:
                for dvr in dvr_list(server):
                    reloaded += 1 if reload_guide(server, dvr["id"]) else 0
        except Exception as e:
            logger.debug(f"Could not reload the guide on {server.get('name')}: {e}")
    if reloaded:
        logger.info(f"Asked {reloaded} media server guide(s) to reload after an EPG refresh")
    return reloaded


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
    # The scan answers before it has finished. Asked for the channels straight away the
    # server has none yet, so there is nothing to switch on and the tuner ends up in a DVR
    # saying "0 enabled", which is exactly what a tuner that does not work looks like.
    channels = _scanned_channels(server, device_id)
    enabled = enable_channels(server, device_id, channels)
    reloaded = _post(server, f"/livetv/dvrs/{dvr_id}/reloadGuide") if dvr_id else False
    return scanned or enabled or reloaded


# How long to wait for a scan to turn up channels, and how often to look
SCAN_SECONDS = 30
SCAN_INTERVAL = 2.0


def _scanned_channels(server, device_id):
    """
    The channels a scan found, once it has found any.

    A scan is started, not done, by the time the request for it comes back, and how long it
    takes depends on how many channels are behind the address. So the server is asked until
    it has some, and given up on rather than waited for forever: a tuner with no channels is
    reported as it is instead of holding everything else up.
    """
    deadline = time.monotonic() + SCAN_SECONDS
    while True:
        channels = device_channels(server, device_id)
        if channels:
            return channels
        if time.monotonic() >= deadline:
            logger.info(
                f"Tuner {device_id} on {server.get('name')} reported no channels within "
                f"{SCAN_SECONDS}s of a scan; press Sync again once it has finished"
            )
            return []
        gevent.sleep(SCAN_INTERVAL)


def refresh_sessions(redis_client, force=False):
    """
    Keep a fresh list of what the media servers are playing, so a stream request never has to
    wait for one. Called from the proxy's cleanup loop; does nothing most times it is called.

    force skips that rate limit, for the one case that cannot wait for the next sweep: a
    request whose viewer is not known yet (see wait_for_device).
    """
    if not redis_client:
        return
    try:
        if not force and not redis_client.set(
            SESSIONS_REFRESH_KEY, "1", nx=True, ex=int(SESSIONS_REFRESH)
        ):
            return
        servers = [server for server in load_servers() if is_enabled(server)]
        if not servers:
            return
        playing = []
        for server in servers:
            playing.extend(sessions(server))
        redis_client.setex(SESSIONS_KEY, SESSIONS_TTL, json.dumps(playing))
        _remember_who_was_watching(redis_client, playing)
    except Exception as e:
        logger.debug(f"Could not refresh the media server sessions: {e}")


# When each device was last seen watching live TV, so a request that arrives before its
# server has registered the session can still be placed (see device_a_moment_ago).
RECENT_DEVICES_KEY = "live:media_servers:recent_devices"
RECENT_DEVICE_SECONDS = 120


def _remember_who_was_watching(redis_client, playing):
    """Note the time against every device on a live channel. Never raises."""
    try:
        now = time.time()
        for session in playing:
            device = session.get("device_id")
            if device and session.get("live"):
                redis_client.hset(RECENT_DEVICES_KEY, device, str(now))
        redis_client.expire(RECENT_DEVICES_KEY, RECENT_DEVICE_SECONDS * 4)
    except Exception as e:
        logger.debug(f"Could not note who was watching: {e}")


# How long to wait for a media server to say who is asking, before giving up and treating
# the request as one from nobody in particular.
DEVICE_WAIT_SECONDS = 2.0
DEVICE_WAIT_STEP = 0.25


def wait_for_device(redis_client, seconds=DEVICE_WAIT_SECONDS):
    """
    Give the media server a moment to say which of its players this request is for.

    A server asks Dispatcharr for the stream and only then registers what it is playing, so
    at the instant the request arrives it can say nothing. Serving it as a viewer we cannot
    tell apart is worse than being a little slower: the overlap does not apply, and the
    channel the viewer just left is not stopped, so its slot stays taken. On a media server,
    where every channel is held open for hours, that is how the slots run out.

    Only waits when nothing else has answered, so it costs nothing on the usual path, and
    gives up after a couple of seconds rather than holding a stream on a server that is
    never going to answer.
    """
    if not redis_client:
        return None
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        # The deadline is checked before asking as well as after: a server that has stopped
        # answering takes its request timeout to fail, and a stream must not be held for
        # that on top of the wait it was promised.
        if time.monotonic() >= deadline:
            return None
        refresh_sessions(redis_client, force=True)
        device = sole_device(redis_client)
        if device:
            return device
        if time.monotonic() >= deadline:
            return None
        gevent.sleep(DEVICE_WAIT_STEP)


def device_a_moment_ago(redis_client):
    """
    The one device that was watching live TV just now, when none is watching this instant.

    A media server asks Dispatcharr for the stream before it has a session to report, so
    there is a moment at the start of every channel where it can say nothing about who is
    asking. Waiting for it would hold up the stream, and treating the viewer as unknown
    costs them the overlap and leaves their old channel running.

    Only answers when exactly one device has been watching, which is the same rule
    sole_device uses: with two there is no way to tell which of them this is.
    """
    try:
        seen = redis_client.hgetall(RECENT_DEVICES_KEY) or {}
        cutoff = time.time() - RECENT_DEVICE_SECONDS
        recent = set()
        for device, when in seen.items():
            device = _as_str(device)
            try:
                if float(_as_str(when)) >= cutoff:
                    recent.add(device)
            except (TypeError, ValueError):
                continue
        return next(iter(recent)) if len(recent) == 1 else None
    except Exception as e:
        logger.debug(f"Could not tell who was watching a moment ago: {e}")
        return None


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

# The channel each device is on, so the one it was on before is known when it moves. This is
# what makes a switch recognisable without the request having said anything about who it is.
DEVICE_CHANNEL_KEY = "live:media_servers:device_channel:{device}"


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

        # This is the first moment the viewer is known for certain, so it is where the
        # overlap's work is really done: the request itself arrived before the server had a
        # session to report, and anything decided then was a guess against the clock.
        previous = _as_str(
            redis_client.get(DEVICE_CHANNEL_KEY.format(device=device))
        )
        redis_client.setex(
            DEVICE_CHANNEL_KEY.format(device=device), CHANNEL_DEVICE_TTL, str(channel_uuid)
        )
        if previous and previous != str(channel_uuid):
            from . import probation

            probation.settle_media_server_start(
                redis_client, channel_uuid, device, previous
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
        # What the channel is called, so a session can be matched to it by what it is
        # playing rather than by when it started
        channel_name = ""
        # What is on it, for the servers that name the programme instead of the channel
        programme_name = ""
        if channel_uuid:
            try:
                from .utils import resolve_channel_display_name

                channel_name = resolve_channel_display_name(
                    channel_uuid, redis_client=redis_client
                ) or ""
            except Exception:
                channel_name = ""
            programme_name = programme_now(channel_uuid)
        deadline = time.time() + WATCH_SECONDS
        # When each stage was first seen, measured from the moment the channel was requested
        seen = {}
        session = None
        position = None
        while time.time() < deadline:
            for server in servers:
                session = _session_for(
                    server, started_at, channel_name, programme_name
                )
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


def _bare_channel_name(name) -> str:
    """
    A channel name with the decoration taken off, for comparing one against another.

    Dispatcharr's names often carry a country in brackets of some kind, which a media server
    does not repeat: "┃FR┃ TFX" on one side and "TFX" on the other are the same channel.
    What is left is compared on its letters and digits, so spacing and case do not matter.

    Only the decoration comes off. "TF1" is not "TF1 Series Films", and treating a name as
    matching anything it is the beginning of would bind a viewer to the wrong channel.
    """
    text = str(name or "")
    for opener, closer in (("┃", "┃"), ("[", "]"), ("(", ")"), ("|", "|")):
        while opener in text and closer in text[text.index(opener) + 1:]:
            start = text.index(opener)
            end = text.index(closer, start + 1)
            text = text[:start] + " " + text[end + 1:]
    return "".join(c for c in text.lower() if c.isalnum())


def _same_channel(reported, channel_name) -> bool:
    """
    Whether a server is naming the channel Dispatcharr handed over.

    The two names are written by different hands, so they are compared with the decoration
    taken off (see _bare_channel_name) rather than exactly.
    """
    ours = _bare_channel_name(channel_name)
    theirs = _bare_channel_name(reported)
    return bool(ours) and ours == theirs


def programme_now(channel_uuid) -> str:
    """
    What Dispatcharr's own guide says is on this channel at the moment.

    Plex does not say which channel a live session is on: it names the programme, and its
    guid says that name came from our XMLTV. So the guide answers the question the session
    does not -- if the server is playing "Le banquet" and our guide has "Le banquet" on the
    channel we just handed over, that is the session.

    Empty when there is no guide for the channel, which is a fallback lost and nothing more.
    """
    try:
        from django.utils import timezone

        from apps.channels.models import Channel
        from apps.epg.models import ProgramData

        channel = (
            Channel.objects.filter(uuid=channel_uuid).only("epg_data_id").first()
        )
        if not channel or not channel.epg_data_id:
            return ""
        now = timezone.now()
        programme = (
            ProgramData.objects.filter(
                epg_id=channel.epg_data_id, start_time__lte=now, end_time__gte=now
            )
            .only("title")
            .first()
        )
        return programme.title if programme else ""
    except Exception as e:
        logger.debug(f"Could not tell what is on channel {channel_uuid}: {e}")
        return ""


def _session_for(server, started_at, channel_name=None, programme_name=None):
    """
    The live session this channel start belongs to: the one playing what we just handed
    over, or failing that one that began at the right moment.

    Two ways to know what it is playing, because the servers say different things. Jellyfin
    names the channel. Plex names the programme, taken from Dispatcharr's own guide, so the
    guide says which channel that programme is on.

    Either is worth preferring because it is a fact about the session. The times are not:
    they come from two clocks that do not agree, and "when it started" means when the
    session began on Plex and when it was last active on Jellyfin, which is refreshed while
    it plays. They are the fallback, not the method.
    """
    live = [session for session in sessions(server) if session["live"]]

    # What it is playing, before when it started. A server says which channel a session is
    # on, and Dispatcharr knows which channel it just handed over, so the two can be put
    # together by name: a fact about the session rather than a guess from two clocks that
    # do not agree and a timestamp that means different things on different servers. Only
    # when exactly one session is on that channel, because with two there is no telling
    # which of them asked.
    if channel_name or programme_name:
        named = [
            session
            for session in live
            if _same_channel(session.get("channel"), channel_name)
            or _same_channel(session.get("title"), channel_name)
            or _same_channel(session.get("title"), programme_name)
        ]
        if len(named) == 1:
            return named[0]

    jellyfin = kind(server) == "jellyfin"
    for session in live:
        if jellyfin:
            # Jellyfin reports when a session was last active, not when it began, and that
            # is refreshed while it plays: matched on time, a stream that started an hour
            # ago looks as new as this one. How far into the stream the player is says it
            # properly, because a session that has only just begun is still near the start.
            if session["position"] is not None and session["position"] <= MATCH_WINDOW:
                return session
            continue
        age = session["started_at"] - started_at
        if -MATCH_BEFORE <= age <= MATCH_WINDOW:
            return session
    return None
