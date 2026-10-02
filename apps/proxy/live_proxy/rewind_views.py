"""Server rewind's API and files (live_proxy/rewind.py)."""

import os

from django.http import FileResponse, HttpResponse, JsonResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny

from apps.accounts.permissions import IsAdmin, IsStandardUser
from dispatcharr.utils import network_access_allowed

from . import rewind


@api_view(["GET", "POST", "DELETE"])
@permission_classes([IsStandardUser])
def rewind_channel(request, channel_uuid):
    """
    GET: what can be rewound into on this channel. POST {viewer, paused_at}: this TV watches it
    (every ~20 s; with where it paused, in wall-clock ms, while paused) -- starts the recording.
    DELETE {viewer}: this TV left the channel.
    """
    viewer = f"{request.user.id}:{str(request.data.get('viewer') or 'tv')[:80]}"
    if request.method == "DELETE":
        rewind.leave(channel_uuid, viewer)
        return JsonResponse({"left": True})
    if request.method == "POST":
        try:
            paused = int(request.data.get("paused_at") or 0) or None
        except (TypeError, ValueError):
            paused = None
        return JsonResponse(rewind.watch(channel_uuid, viewer, paused))
    return JsonResponse(rewind.window(channel_uuid))


@api_view(["GET"])
@permission_classes([IsAdmin])
def rewind_usage(request):
    """What is recorded now and its size, for the arrTV settings page."""
    conf = rewind.settings()
    found = rewind.usage()
    return JsonResponse({
        "settings": conf, "channels": found,
        "bytes": sum(c["bytes"] for c in found), "budget_bytes": rewind.budget_bytes(conf),
        "folder": rewind.rewind_dir(),
    })


@api_view(["GET"])
@permission_classes([AllowAny])
def rewind_playlist(request, channel_uuid):
    if not network_access_allowed(request, "STREAMS"):
        return JsonResponse({"error": "Forbidden"}, status=403)
    if not rewind.segments(channel_uuid):
        return JsonResponse({"error": "Nothing recorded for this channel"}, status=404)
    response = HttpResponse(rewind.playlist(channel_uuid), content_type="application/vnd.apple.mpegurl")
    response["Cache-Control"] = "no-cache"
    return response


@api_view(["GET"])
@permission_classes([AllowAny])
def rewind_segment(request, channel_uuid, name):
    if not network_access_allowed(request, "STREAMS"):
        return JsonResponse({"error": "Forbidden"}, status=403)
    path = rewind.segment_path(channel_uuid, name)
    if path is None:
        return JsonResponse({"error": "Gone"}, status=404)
    response = FileResponse(open(path, "rb"), content_type="video/mp2t")
    response["Content-Length"] = str(os.path.getsize(path))
    return response
