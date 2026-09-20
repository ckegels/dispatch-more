"""The Channel Manager page: what can be chosen, the plan for what is chosen, and applying
the rows picked from it. See channel_manager for how channels are recognised."""

import logging

from django.db.models import Count
from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.permissions import IsAdmin

from . import channel_manager

logger = logging.getLogger(__name__)


@api_view(["GET"])
@permission_classes([IsAdmin])
def channel_manager_options(request):
    """Everything the levers choose between, with enough about each to choose well."""
    from apps.m3u.models import M3UAccount

    from .models import Channel, ChannelGroup, ChannelProfile, Stream

    stream_groups = (
        Stream.objects.filter(channel_group__isnull=False)
        .values("channel_group_id", "channel_group__name")
        .annotate(streams=Count("id"))
        .order_by("channel_group__name")
    )
    channel_groups = (
        Channel.objects.filter(channel_group__isnull=False)
        .values("channel_group_id", "channel_group__name")
        .annotate(channels=Count("id"))
        .order_by("channel_group__name")
    )
    return JsonResponse({
        "settings": channel_manager.load_settings(),
        "defaults": channel_manager.DEFAULTS,
        "accounts": [
            {"id": a.id, "name": a.name, "active": a.is_active}
            for a in M3UAccount.objects.order_by("name")
        ],
        "stream_groups": [
            {"id": g["channel_group_id"], "name": g["channel_group__name"], "count": g["streams"]}
            for g in stream_groups
        ],
        "channel_groups": [
            {"id": g["channel_group_id"], "name": g["channel_group__name"], "count": g["channels"]}
            for g in channel_groups
        ],
        # A new channel may go into a group that has none yet
        "all_groups": [
            {"id": g.id, "name": g.name} for g in ChannelGroup.objects.order_by("name")
        ],
        "profiles": [
            {"id": p.id, "name": p.name} for p in ChannelProfile.objects.order_by("name")
        ],
    })


@api_view(["POST"])
@permission_classes([IsAdmin])
def channel_manager_preview(request):
    """The plan for the settings given, over the saved ones. Nothing is written."""
    settings = channel_manager.settings_from(request.data.get("settings"))
    return JsonResponse(channel_manager.build_plan(settings))


@api_view(["POST"])
@permission_classes([IsAdmin])
def channel_manager_apply(request):
    """
    Carry out the rows chosen, worked out again from the settings rather than taken from
    the page, so what is applied is what is true now.
    """
    keys = request.data.get("keys")
    if not isinstance(keys, list) or not keys:
        return JsonResponse({"error": "Choose at least one channel to apply"}, status=400)
    settings = channel_manager.settings_from(request.data.get("settings"))
    return JsonResponse(channel_manager.apply_plan(
        settings, keys, request.data.get("orders"), request.data.get("groups"), request.data.get("drops"),
        request.data.get("names"), request.data.get("epgs"),
    ))


@api_view(["GET"])
@permission_classes([IsAdmin])
def channel_manager_guides(request):
    """
    The guides one channel could be, for the picker on its row: the best matches for the
    name given, or -- with `q` -- a plain search through every guide there is. Asked for
    one row at a time, when it is opened, because matching this well over every channel
    at once would keep the whole page waiting.
    """
    return JsonResponse({"guides": channel_manager.guide_candidates(
        request.GET.get("name", ""),
        request.GET.get("tvg_id", ""),
        request.GET.get("q", ""),
        request.GET.get("limit", 12),
    )})


@api_view(["POST"])
@permission_classes([IsAdmin])
def channel_manager_ignore(request):
    """
    Stop suggesting a row ("ignore"), suggest one again ("unignore"), or everything ignored
    again ("clear").
    """
    action = request.data.get("action")
    key = str(request.data.get("key") or "")
    if action == "clear":
        channel_manager.unignore()
        return JsonResponse({"ignored": 0})
    if not key:
        return JsonResponse({"error": "Which suggestion?"}, status=400)
    if action == "unignore":
        return JsonResponse({"ignored": channel_manager.unignore(key)})
    if action != "ignore":
        return JsonResponse({"error": f"Unknown action: {action}"}, status=400)
    try:
        streams = [int(i) for i in request.data.get("streams") or ()]
    except (TypeError, ValueError):
        return JsonResponse({"error": "Streams are given by id"}, status=400)
    entry = channel_manager.ignore(
        key, str(request.data.get("name") or ""), str(request.data.get("kind") or ""), streams
    )
    return JsonResponse({"ignored": entry})


@api_view(["PUT"])
@permission_classes([IsAdmin])
def channel_manager_settings(request):
    """Keep the levers as they are, so the page opens the way it was left."""
    saved = channel_manager.save_settings(
        {**channel_manager.load_settings(), **(request.data.get("settings") or {})}
    )
    return JsonResponse({"settings": saved})
