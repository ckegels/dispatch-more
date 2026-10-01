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
