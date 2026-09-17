"""The Media Servers settings tab: add a server, see whether it answers, and what it is playing.

Read-only towards the media server: nothing here changes anything on Plex. The token is never
sent back to the browser (see media_servers.public).
"""

import logging

from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.permissions import IsAdmin

from . import media_servers
from . import recovery

logger = logging.getLogger("live_proxy")


@api_view(["GET", "POST"])
@permission_classes([IsAdmin])
def stream_recovery(request):
    """Stream Recovery: keeping a channel alive when a working connection is closed."""
    if request.method == "GET":
        return JsonResponse({"settings": recovery.settings(), "scopes": list(recovery.SCOPES)})

    values = dict(recovery.settings())
    try:
        if "enabled" in request.data:
            values["enabled"] = bool(request.data["enabled"])
        if "stable_seconds" in request.data:
            seconds = int(request.data["stable_seconds"])
            if not 5 <= seconds <= 600:
                raise ValueError("Give a number of seconds between 5 and 600")
            values["stable_seconds"] = seconds
        if "max_per_hour" in request.data:
            per_hour = int(request.data["max_per_hour"])
            if not 1 <= per_hour <= 120:
                raise ValueError("Give a number between 1 and 120")
            values["max_per_hour"] = per_hour
        if "scope" in request.data:
            if request.data["scope"] not in recovery.SCOPES:
                raise ValueError("Choose which channels this applies to")
            values["scope"] = request.data["scope"]
    except (TypeError, ValueError) as e:
        return JsonResponse({"error": str(e)}, status=400)

    recovery.save_settings(values)
    logger.info(f"Stream Recovery settings saved: {values}")
    return JsonResponse({"settings": values, "scopes": list(recovery.SCOPES)})


def _server_rows():
    """Every server, with whether it answers and what it is playing right now."""
    rows = []
    for server in media_servers.load_servers():
        row = media_servers.public(server)
        if not media_servers.is_enabled(server):
            # Switched off: nothing is asked of it, so there is nothing to report
            row.update({"online": False, "error": "", "server_name": "", "version": "",
                        "sessions": []})
            rows.append(row)
            continue
        status = media_servers.check(server)
        row["online"] = status["ok"]
        row["error"] = status.get("error", "")
        row["server_name"] = status.get("name", "")
        row["version"] = status.get("version", "")
        row["sessions"] = media_servers.sessions(server) if status["ok"] else []
        rows.append(row)
    return rows


@api_view(["GET", "POST", "DELETE"])
@permission_classes([IsAdmin])
def media_server_list(request):
    """List the media servers, add or change one, or remove one."""
    if request.method == "GET":
        return JsonResponse({"servers": _server_rows()})

    if request.method == "DELETE":
        server_id = request.query_params.get("id")
        servers = [s for s in media_servers.load_servers() if s.get("id") != server_id]
        media_servers.save_servers(servers)
        return JsonResponse({"servers": _server_rows()})

    servers = media_servers.load_servers()
    # Switching one off (or on) changes nothing else about it, and never needs the server
    if "enabled" in request.data and len(request.data) <= 2:
        server_id = request.data.get("id")
        existing = next((s for s in servers if s.get("id") == server_id), None)
        if existing is None:
            return JsonResponse({"error": "No such media server"}, status=404)
        existing["enabled"] = bool(request.data.get("enabled"))
        media_servers.save_servers(servers)
        logger.info(
            f"Media server {existing.get('name')} "
            f"{'enabled' if existing['enabled'] else 'switched off'}"
        )
        return JsonResponse({"servers": _server_rows()})

    url = media_servers.clean_url(request.data.get("url"))
    if not url.startswith(("http://", "https://")):
        return JsonResponse(
            {"error": "Enter the address as http://… or https://…"}, status=400
        )

    server_id = request.data.get("id")
    existing = next((s for s in servers if s.get("id") == server_id), None)
    token = (request.data.get("token") or "").strip()
    if not token and existing:
        # Editing without retyping the token keeps the one that is stored
        token = existing.get("token", "")
    if not token:
        return JsonResponse(
            {"error": "A token (Plex) or API key (Jellyfin) is needed"}, status=400
        )

    # Built from the stored one, so editing a name keeps everything that was learned or
    # switched about it: whether it is in use, and the address its tuners were added with.
    server = dict(existing or {})
    server.update({
        "id": server_id or media_servers.new_id(),
        "kind": (request.data.get("kind") or server.get("kind") or "plex").lower(),
        "name": (request.data.get("name") or "").strip() or server.get("name") or "Plex",
        "url": url,
        "token": token,
    })
    server.setdefault("enabled", True)
    status = media_servers.check(server)
    if not status["ok"]:
        # Nothing is saved when it cannot be reached, so a wrong address cannot be stored
        return JsonResponse({"error": status["error"]}, status=400)

    if existing:
        servers[servers.index(existing)] = server
    else:
        servers.append(server)
    media_servers.save_servers(servers)
    logger.info(f"Media server {server['name']} ({server['url']}) saved")
    return JsonResponse({"servers": _server_rows()})
