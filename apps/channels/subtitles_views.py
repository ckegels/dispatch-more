"""The Subtitles tab (fork/subtitles.md step 1): what every channel's streams carry."""

from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.permissions import IsAdmin

from . import subtitles


@api_view(["GET", "PUT"])
@permission_classes([IsAdmin])
def subtitles_page(request):
    """GET: every channel and the subtitles its streams carry. PUT {channel, spoken}: the
    language a channel speaks, set by hand ("" goes back to the guess)."""
    if request.method == "PUT":
        try:
            subtitles.set_spoken(int(request.data.get("channel")), str(request.data.get("spoken") or ""))
        except (TypeError, ValueError):
            return JsonResponse({"error": "Which channel?"}, status=400)
    found = subtitles.rows()
    return JsonResponse({"rows": found, "summary": subtitles.summary(found)})


@api_view(["GET", "PUT"])
@permission_classes([IsAdmin])
def captions_page(request):
    """GET: the caption worker, what this server has and what fits it (fork/subtitles.md §5b).
    PUT: the captions settings."""
    from .captions import manager

    if request.method == "PUT":
        try:
            manager.save({k: v for k, v in request.data.items() if not (k == "token" and v and set(v) == {"•"})})
        except (TypeError, ValueError):
            return JsonResponse({"error": "Those settings are not right."}, status=400)
    return JsonResponse(manager.status())


@api_view(["POST"])
@permission_classes([IsAdmin])
def captions_action(request):
    """{action: install|remove} for the root watcher, or {action: look|download|benchmark,
    model} for the worker."""
    from .captions import manager

    action = str(request.data.get("action") or "")
    try:
        if action in ("install", "remove"):
            manager.request(action, getattr(request.user, "username", ""))
        else:
            manager.worker_action(action, str(request.data.get("model") or ""))
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse(manager.status())
