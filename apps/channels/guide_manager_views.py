"""The Guides tab: what it suggests, how a run is going, and putting a guide on a channel.

See guide_manager for what counts as worth suggesting and why the looking runs in batches.
"""

import logging

from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.permissions import IsAdmin

from . import guide_manager

logger = logging.getLogger(__name__)


@api_view(["GET"])
@permission_classes([IsAdmin])
def guide_manager_page(request):
    """Everything the page draws: the settings, what was found, and how a run is going."""
    from django.db.models import Count

    from .models import Channel, ChannelGroup

    groups = (
        Channel.objects.filter(channel_group__isnull=False)
        .values("channel_group_id", "channel_group__name")
        .annotate(channels=Count("id"))
        .order_by("channel_group__name")
    )
    ignored = guide_manager.load_ignored()
    chosen = guide_manager.load_chosen()
    stored = guide_manager.load_suggestions()
    # "Every channel" means every channel, not every channel the last run reached
    if request.GET.get("all"):
        found = guide_manager.every_channel(guide_manager.load_settings(), stored)
    else:
        found = list(stored.values())
    # As the guides are now, not as the run left them: reading a guide's programmes
    # afterwards does not go back and change what it wrote down
    guide_manager.freshen(found)
    # Whether a channel is settled is asked now rather than taken from what a run wrote:
    # settling one and a run looking at it happen in either order
    guide_manager.mark_chosen(found, chosen)
    guide_manager.mark_waved_away(found, ignored)
    return JsonResponse({
        "settings": guide_manager.load_settings(),
        "defaults": guide_manager.DEFAULTS,
        "suggestions": found,
        "run": guide_manager.run_state(guide_manager.redis()),
        "channel_groups": [
            {"id": g["channel_group_id"], "name": g["channel_group__name"], "count": g["channels"]}
            for g in groups
        ],
        "all_groups": [{"id": g.id, "name": g.name} for g in ChannelGroup.objects.order_by("name")],
        "ignored": [{"channel": k, **v} for k, v in ignored.items()],
        "chosen": [{"channel": k, **v} for k, v in chosen.items()],
    })


@api_view(["POST"])
@permission_classes([IsAdmin])
def guide_manager_run(request):
    """Start looking, or ask a run to stop after the batch it is in."""
    action = request.data.get("action", "start")
    if action == "stop":
        return JsonResponse(guide_manager.stop(guide_manager.redis()))
    if action != "start":
        return JsonResponse({"error": f"Unknown action: {action}"}, status=400)
    settings = guide_manager.settings_from(request.data.get("settings"))
    return JsonResponse(guide_manager.start(settings, guide_manager.redis()))


@api_view(["POST"])
@permission_classes([IsAdmin])
def guide_manager_apply(request):
    """Put the guides chosen on their channels: {channel id: guide id, or null for none}."""
    choices = request.data.get("choices")
    if not isinstance(choices, dict) or not choices:
        return JsonResponse({"error": "Choose at least one channel"}, status=400)
    return JsonResponse(guide_manager.apply(choices))


@api_view(["POST"])
@permission_classes([IsAdmin])
def guide_manager_ignore(request):
    """Wave a suggestion away ("ignore"), take one back ("unignore"), or all ("clear")."""
    action = request.data.get("action")
    if action == "clear":
        guide_manager.unignore()
        return JsonResponse({"ignored": 0})
    channel = request.data.get("channel")
    if channel in (None, ""):
        return JsonResponse({"error": "Which channel?"}, status=400)
    if action == "unignore":
        return JsonResponse({"ignored": guide_manager.unignore(channel)})
    if action != "ignore":
        return JsonResponse({"error": f"Unknown action: {action}"}, status=400)
    entry = guide_manager.ignore(
        channel, str(request.data.get("name") or ""), request.data.get("epg")
    )
    # Off the list it is on now, as well as the ones made later
    guide_manager.drop_suggestion(channel)
    return JsonResponse({"ignored": entry})


@api_view(["POST"])
@permission_classes([IsAdmin])
def guide_manager_chosen(request):
    """Settle a channel's guide ("choose"), unsettle one ("unchoose"), or all ("clear")."""
    action = request.data.get("action")
    if action == "clear":
        guide_manager.unchoose()
        return JsonResponse({"chosen": 0})
    channel = request.data.get("channel")
    if channel in (None, ""):
        return JsonResponse({"error": "Which channel?"}, status=400)
    if action == "unchoose":
        return JsonResponse({"chosen": guide_manager.unchoose(channel)})
    if action != "choose":
        return JsonResponse({"error": f"Unknown action: {action}"}, status=400)
    entry = guide_manager.choose(
        channel, str(request.data.get("name") or ""), request.data.get("epg")
    )
    # Off the list it is on now as well, so settling one takes it out of what is being
    # put forward rather than only out of what the next run puts forward
    guide_manager.drop_suggestion(channel)
    return JsonResponse({"chosen": entry})


@api_view(["PUT"])
@permission_classes([IsAdmin])
def guide_manager_settings(request):
    """Keep the settings, so the page opens the way it was left."""
    try:
        saved = guide_manager.save_settings(request.data.get("settings"))
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse({"settings": saved})
