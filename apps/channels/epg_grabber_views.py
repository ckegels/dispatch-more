"""The EPG Grabber tab: where iptv-org/epg is, what to grab from it, and when.

See epg_grabber for what it does with what comes out.
"""

import logging

from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.permissions import IsAdmin
from core.utils import RedisClient

from . import epg_grabber

logger = logging.getLogger(__name__)


@api_view(["GET"])
@permission_classes([IsAdmin])
def epg_grabber_page(request):
    """Everything the tab draws: the settings, what is there, and how a grab is going."""
    from apps.epg.models import EPGSource

    redis_client = RedisClient.get_client()
    settings = epg_grabber.load_settings()
    return JsonResponse({
        "settings": settings,
        "defaults": epg_grabber.DEFAULTS,
        "job_defaults": epg_grabber.JOB_DEFAULTS,
        # Whether the grabber is where it is said to be, and what it holds
        "install": epg_grabber.look_at_it(settings),
        # The channel lists it has, so a guide can be set up without typing paths
        "channel_files": epg_grabber.channel_files(settings),
        "running": epg_grabber.is_running(redis_client),
        "progress": epg_grabber.progress(redis_client),
        # Which of Dispatcharr's EPG sources a guide can be handed to
        "epg_sources": [
            {"id": one.id, "name": one.name, "file_path": one.file_path or "", "url": one.url or ""}
            for one in EPGSource.objects.order_by("name")
        ],
    })


@api_view(["PUT"])
@permission_classes([IsAdmin])
def epg_grabber_settings(request):
    try:
        saved = epg_grabber.save_settings(request.data.get("settings") or {})
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse({"settings": saved, "install": epg_grabber.look_at_it(saved)})


@api_view(["POST"])
@permission_classes([IsAdmin])
def epg_grabber_run(request):
    """Grab now: every guide that is set up, or the one given. Stop asks it to stop."""
    from .tasks import run_epg_grab

    redis_client = RedisClient.get_client()
    if request.data.get("action") == "stop":
        return JsonResponse({"stopping": epg_grabber.request_stop(redis_client)})
    if epg_grabber.is_running(redis_client):
        return JsonResponse(
            {"error": "A grab is already running. Thousands of requests twice over is worse, not faster."},
            status=409,
        )
    only = request.data.get("job")
    found = epg_grabber.look_at_it()
    if not found["ok"]:
        return JsonResponse({"error": found["why"]}, status=400)
    run_epg_grab.delay(only=str(only) if only else None)
    return JsonResponse({"started": True})


@api_view(["POST"])
@permission_classes([IsAdmin])
def epg_grabber_channel_list(request):
    """
    A channel list made out of a bigger one: the entries that say a word.

    Without "apply" it says what would be kept and shows the first few, because a word
    that matches four thousand channels or four is worth finding out before the scrape
    rather than during it.
    """
    try:
        found = epg_grabber.make_list(
            str(request.data.get("from") or ""),
            str(request.data.get("keep") or ""),
            str(request.data.get("leave_out") or ""),
            into=str(request.data.get("into") or ""),
            write=bool(request.data.get("apply")),
        )
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse(found)


@api_view(["POST"])
@permission_classes([IsAdmin])
def epg_grabber_source(request):
    """
    Make an EPG source for a guide this grabs, so there is nothing to set up by hand.

    A source with a file and no URL is read from disk by Dispatcharr, which is why no web
    server is needed in between. It is made switched off with no refresh of its own: this
    tab refreshes it when there is a new guide to read.
    """
    from apps.epg.models import EPGSource

    name = str(request.data.get("name") or "").strip()
    output = str(request.data.get("output") or "").strip()
    if not name or not output:
        return JsonResponse({"error": "A name, and the file it reads"}, status=400)
    source, made = EPGSource.objects.get_or_create(
        name=name,
        defaults={"source_type": "xmltv", "file_path": output, "is_active": True, "refresh_interval": 0},
    )
    if not made and source.file_path != output:
        source.file_path = output
        source.save(update_fields=["file_path"])
    logger.info(f"EPG grabber: {'made' if made else 'pointed'} EPG source {name} at {output}")
    return JsonResponse({"id": source.id, "name": source.name, "made": made})
