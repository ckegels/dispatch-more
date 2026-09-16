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
import time

import gevent
import requests

logger = logging.getLogger("live_proxy")

SETTINGS_KEY = "media-servers"
REQUEST_TIMEOUT = 5

# How long, and how often, sessions are watched after a channel start. Plex needs a moment to
# create the session, and the buffering we are measuring is a handful of seconds.
WATCH_SECONDS = 25
WATCH_INTERVAL = 1.0
# A session belongs to this start when it appeared around the same time
MATCH_WINDOW = 15


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


def public(server):
    """A server as the browser may see it: everything except the token."""
    return {
        "id": server.get("id"),
        "name": server.get("name") or "Plex",
        "kind": server.get("kind", "plex"),
        "url": server.get("url", ""),
        "has_token": bool(server.get("token")),
    }


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


def watch_start(redis_client, start_id, user_agent, started_at=None):
    """
    After a channel start on a media server, watch its sessions for a moment and add what the
    server did to the start (see timing.finish). Returns at once; the watching runs in the
    background and never touches the stream.
    """
    try:
        if not start_id or not _is_media_server(user_agent) or not load_servers():
            return
        gevent.spawn(_watch, redis_client, start_id, started_at or time.time())
    except Exception as e:
        logger.debug(f"Could not watch a media server for start {start_id}: {e}")


def _is_media_server(user_agent) -> bool:
    from . import probation

    return probation.is_media_server(user_agent)


def _watch(redis_client, start_id, started_at):
    """Poll until the session is playing, or until it has been long enough."""
    from django.db import close_old_connections

    from . import timing

    try:
        servers = load_servers()
        deadline = time.time() + WATCH_SECONDS
        buffering_since = None
        while time.time() < deadline:
            gevent.sleep(WATCH_INTERVAL)
            session = None
            for server in servers:
                session = _session_for(server, started_at)
                if session:
                    break
            if not session:
                continue
            if session["state"] == "buffering":
                buffering_since = buffering_since or time.time()
                continue
            if session["state"] == "playing":
                timing.update_start(
                    redis_client,
                    start_id,
                    server_user=session["user"],
                    server_player=session["player"],
                    server_decision=session["decision"],
                    server_speed=f"{session['speed']:.1f}" if session["speed"] else "",
                    # How long the player spent buffering before it played anything
                    server_buffering=f"{max(time.time() - started_at, 0):.1f}",
                )
                return
        logger.debug(f"No media server session became playable for start {start_id}")
    except Exception as e:
        logger.debug(f"Could not follow a media server session for start {start_id}: {e}")
    finally:
        try:
            close_old_connections()
        except Exception:
            pass


def _session_for(server, started_at):
    """The live session that started around the same moment as this channel start."""
    for session in sessions(server):
        if not session["live"]:
            continue
        if abs(session["started_at"] - started_at) <= MATCH_WINDOW:
            return session
    return None
