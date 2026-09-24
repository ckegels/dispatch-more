"""The Stream Check tab: what was found, the settings, runs, and what is done about a
broken stream. See stream_check for how streams are looked at."""

import logging
import shutil

from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.permissions import IsAdmin
from core.utils import RedisClient

from . import stream_check

logger = logging.getLogger(__name__)

SHOWS = ("problems", "broken", "all")


@api_view(["GET"])
@permission_classes([IsAdmin])
def stream_check_overview(request):
    """Everything the tab shows: the channels with a stream that does not play, the parked
    streams, how a run is going, and the settings."""
    from .models import ChannelGroup

    redis_client = RedisClient.get_client()
    show = request.GET.get("show", "problems")
    try:
        keep = {int(one) for one in (request.GET.get("keep") or "").split(",") if one.strip()}
    except (TypeError, ValueError):
        keep = set()
    found = stream_check.issues(redis_client, show if show in SHOWS else "problems", keep)
    return JsonResponse({
        **found,
        "settings": stream_check.load_settings(),
        "progress": stream_check.progress(redis_client),
        "running": stream_check.is_running(redis_client),
        "last_run": stream_check.load_results()["last_run"],
        # Without it a stream is judged by whether MPEG-TS keeps coming, which is weaker
        "ffprobe": bool(shutil.which("ffprobe")),
        # What each provider allows, learned or set by hand
        "limits": stream_check.provider_limits(redis_client),
        "channel_groups": [
            {"id": g.id, "name": g.name}
            for g in ChannelGroup.objects.filter(channels__isnull=False).distinct().order_by("name")
        ],
    })


@api_view(["POST"])
@permission_classes([IsAdmin])
def stream_check_run(request):
    """Start a run now: every stream, or the ones given (a stream checked again from the
    page). Still only while nobody is watching."""
    from .tasks import run_stream_check

    redis_client = RedisClient.get_client()
    only = request.data.get("only")
    if stream_check.is_running(redis_client) and only is None:
        return JsonResponse(
            {"error": "A check is already running. Try again once it is done."}, status=409
        )
    if only is not None:
        try:
            only = [int(i) for i in only]
        except (TypeError, ValueError):
            return JsonResponse({"error": "Streams are given by id"}, status=400)
        run_stream_check.delay(only=only)
        return JsonResponse({"started": True, "streams": len(only)})
    total = stream_check.start_round(redis_client, force=True)
    run_stream_check.delay()
    return JsonResponse({"started": True, "streams": total})


@api_view(["POST"])
@permission_classes([IsAdmin])
def stream_check_stop(request):
    return JsonResponse({"stopping": stream_check.request_stop(RedisClient.get_client())})


@api_view(["PUT"])
@permission_classes([IsAdmin])
def stream_check_settings(request):
    try:
        saved = stream_check.save_settings(request.data.get("settings") or {})
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse({"settings": saved})


@api_view(["POST"])
@permission_classes([IsAdmin])
def stream_check_clear(request):
    """Forget what the runs found, for results that cannot be trusted. Parked stay parked."""
    if stream_check.is_running(RedisClient.get_client()):
        return JsonResponse({"error": "Stop the check first"}, status=409)
    stream_check.clear_results()
    return JsonResponse({"cleared": True})


@api_view(["PUT"])
@permission_classes([IsAdmin])
def stream_check_limit(request):
    """Set a provider's limit by hand, or with no limit, forget it so it is learned again."""
    key = str(request.data.get("key") or "")
    if not key:
        return JsonResponse({"error": "Which provider?"}, status=400)
    try:
        limit = int(request.data["limit"]) if request.data.get("limit") not in (None, "") else None
        minutes = float(request.data.get("window_minutes") or 10)
    except (TypeError, ValueError):
        return JsonResponse({"error": "Numbers only, please"}, status=400)
    if limit is not None and (limit < 1 or minutes <= 0):
        return JsonResponse({"error": "At least one stream, over some minutes"}, status=400)
    kept = stream_check.set_limit(key, str(request.data.get("name") or key), limit, minutes)
    return JsonResponse({"limit": kept})


ACTIONS = {
    # Off this channel, or every channel, for good
    "remove": lambda data: stream_check.remove(data["stream_id"], data.get("channel_id")),
    # Off every channel, remembered, checked again on every run
    "park": lambda data: stream_check.park(data["stream_id"], data.get("reason", "")),
    # A parked stream back where it was
    "restore": lambda data: stream_check.restore(data["stream_id"]),
    # A parked stream no longer kept: it stays off its channels
    "forget": lambda data: stream_check.forget(data["stream_id"]),
    # Leave alone: off the list and not checked again; nothing on the channels changes
    "ignore": lambda data: stream_check.ignore(data["stream_id"]),
    "unignore": lambda data: stream_check.unignore(data["stream_id"]),
}


@api_view(["POST"])
@permission_classes([IsAdmin])
def stream_check_action(request):
    action = request.data.get("action")
    if action not in ACTIONS:
        return JsonResponse({"error": f"Unknown action: {action}"}, status=400)
    # One stream, or every stream of a channel at once. Both kinds come through here
    # because a channel whose streams are all broken is parked a channel at a time, not a
    # stream at a time -- and doing it one at a time took the row out from under you.
    given = request.data.get("stream_ids")
    if given is None:
        given = [request.data.get("stream_id")]
    try:
        stream_ids = [int(one) for one in given]
        channel_id = int(request.data["channel_id"]) if request.data.get("channel_id") else None
        reason = str(request.data.get("reason") or "")
    except (KeyError, TypeError, ValueError):
        return JsonResponse({"error": "A stream is given by id"}, status=400)
    if not stream_ids:
        return JsonResponse({"error": "No streams were given"}, status=400)

    changed = 0
    done = []
    for stream_id in stream_ids:
        data = {"stream_id": stream_id, "channel_id": channel_id, "reason": reason}
        try:
            changed += ACTIONS[action](data) or 0
            done.append(stream_id)
        except ValueError as e:
            # One stream that cannot be done does not stop the rest: with every stream of
            # a channel going at once, stopping half way is the worst of both
            logger.warning(f"Stream Check: could not {action} stream {stream_id}: {e}")
    if not done:
        return JsonResponse({"error": f"Could not {action} those streams"}, status=400)
    logger.info(f"Stream Check: {action} {len(done)} stream(s) ({changed} channels)")
    return JsonResponse({"done": action, "channels": changed, "streams": done})


@api_view(["POST"])
@permission_classes([IsAdmin])
def stream_check_channels(request):
    """
    Whole channels at once: {"action": "park" | "remove", "channel_ids": [...]}.

    Park takes every provider stream off them -- the backups too -- and keeps them in
    Parked with where they were. Remove deletes the channels. The page asks first.
    """
    action = request.data.get("action")
    try:
        channel_ids = [int(one) for one in request.data.get("channel_ids") or []]
    except (TypeError, ValueError):
        return JsonResponse({"error": "Channels are given by id"}, status=400)
    if not channel_ids:
        return JsonResponse({"error": "No channels were given"}, status=400)
    if action == "park":
        return JsonResponse({"done": "park", **stream_check.park_channels(channel_ids)})
    if action == "remove":
        return JsonResponse({"done": "remove", **stream_check.remove_channels(channel_ids)})
    return JsonResponse({"error": f"Unknown action: {action}"}, status=400)
