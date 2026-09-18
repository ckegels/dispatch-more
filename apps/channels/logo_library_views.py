"""The logo library tab: what each channel has, what the collections would give it, and
applying what is chosen. See logo_library for where the logos come from and how a channel
is matched to one."""

import logging

from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.permissions import IsAdmin

from . import logo_library

logger = logging.getLogger(__name__)

# More than this in one apply is refused: it is a list someone looked at, not a bulk job
MAX_APPLY = 5000


def _status(index):
    if not index:
        return {"built": False, "counts": {}, "errors": {}, "built_at": None}
    return {
        "built": True,
        "counts": index.get("counts") or {},
        "errors": index.get("errors") or {},
        "built_at": index.get("built_at"),
    }


@api_view(["GET"])
@permission_classes([IsAdmin])
def logo_library_suggestions(request):
    """
    Every channel, with the logo it has and the ones the collections would give it.

    ?show=suggested (the default) lists only channels something was found for, which is
    the list worth working through. ?show=missing lists channels with no logo at all, and
    ?show=all lists everything. ?search= narrows by name.
    """
    from .models import Channel

    index = logo_library.load_index()
    show = request.query_params.get("show") or "suggested"
    search = (request.query_params.get("search") or "").strip().lower()

    rows = []
    if index:
        channels = Channel.objects.select_related("logo").order_by("channel_number", "name")
        for channel in channels:
            if search and search not in (channel.name or "").lower():
                continue
            current = (
                {"id": channel.logo.id, "name": channel.logo.name, "url": channel.logo.url}
                if channel.logo_id
                else None
            )
            if show == "missing" and current:
                continue
            suggestions = logo_library.suggestions_for(channel.name, index)
            # A suggestion that is already the logo it has is nothing to suggest
            if current:
                suggestions = [s for s in suggestions if s["url"] != current["url"]]
            if show == "suggested" and not suggestions:
                continue
            rows.append({
                "channel_id": channel.id,
                "number": channel.channel_number,
                "name": channel.name,
                "country": logo_library.country_of(channel.name),
                "current": current,
                "suggestions": suggestions,
            })

    return JsonResponse({"status": _status(index), "channels": rows})


@api_view(["POST"])
@permission_classes([IsAdmin])
def logo_library_refresh(request):
    """
    Download the collections again, in the background: they are several megabytes, which
    is too long to hold a request open for. The page asks for the status until it changes.
    """
    from .tasks import build_logo_library

    task = build_logo_library.delay()
    return JsonResponse({"started": True, "task_id": task.id})


@api_view(["GET"])
@permission_classes([IsAdmin])
def logo_library_status(request):
    return JsonResponse(_status(logo_library.load_index()))


@api_view(["POST"])
@permission_classes([IsAdmin])
def logo_library_apply(request):
    """
    Give channels the logos chosen for them.

    Takes [{"channel_id", "url", "name"}]. Only what is sent changes: nothing is applied
    that was not chosen on the page.
    """
    chosen = request.data.get("assignments")
    if not isinstance(chosen, list) or not chosen:
        return JsonResponse({"error": "Choose at least one logo to apply"}, status=400)
    if len(chosen) > MAX_APPLY:
        return JsonResponse(
            {"error": f"No more than {MAX_APPLY} at once"}, status=400
        )
    try:
        assignments = [
            (int(item["channel_id"]), str(item["url"]), str(item.get("name") or ""))
            for item in chosen
        ]
    except (KeyError, TypeError, ValueError):
        return JsonResponse(
            {"error": "Each logo needs a channel_id and a url"}, status=400
        )
    return JsonResponse(logo_library.apply_logos(assignments))
