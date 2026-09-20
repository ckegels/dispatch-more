"""The Channel Manager page: what can be chosen, the plan for what is chosen, and applying
the rows picked from it. See channel_manager for how channels are recognised."""

import logging

from django.db.models import Count, Q
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
    # Every group, each said to be one of four kinds, and the page shows the kinds the
    # levers ask for. Which is the point: every group there is runs to hundreds on a real
    # setup, nearly all of them a provider's own names that no channel is in, and which of
    # those you want to see is not something to decide for somebody.
    #
    #   with_channels  you have channels in it
    #   empty          nothing in it at all, so somebody made it by hand -- very likely a
    #                  moment ago on this page
    #   active_m3u     a provider's group, carrying streams of an account switched on
    #   inactive_m3u   the same, of an account switched off
    #
    # Not "channels"/"streams" as annotation names: they are the relations themselves.
    def kind_of(group):
        if group.how_many:
            return "with_channels"
        if not group.their_streams:
            return "empty"
        return "active_m3u" if group.live_streams else "inactive_m3u"

    channel_groups = [
        {
            "channel_group_id": g.id,
            "channel_group__name": g.name,
            "channels": g.how_many,
            "streams": g.their_streams,
            "kind": kind_of(g),
        }
        for g in ChannelGroup.objects.annotate(
            how_many=Count("channels", distinct=True),
            their_streams=Count("streams", distinct=True),
            live_streams=Count(
                "streams", filter=Q(streams__m3u_account__is_active=True), distinct=True
            ),
        ).order_by("name")
    ]
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
            {
                "id": g["channel_group_id"], "name": g["channel_group__name"],
                "count": g["channels"], "streams": g["streams"], "kind": g["kind"],
            }
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
        request.GET.get("current", ""),
    )})


@api_view(["POST"])
@permission_classes([IsAdmin])
def channel_manager_load_guide(request):
    """
    Read these guides' programmes now, so they can be looked at before one is chosen.
    Dispatcharr only reads them once a guide is on a channel. Several at once because
    reading one costs a pass of the whole file either way (see load_programmes).
    """
    given = request.data.get("ids")
    if given is None:
        given = [request.data.get("id")]
    if not isinstance(given, list) or not given:
        return JsonResponse({"error": "Which guides?"}, status=400)
    answer = channel_manager.load_programmes(given)
    return JsonResponse(answer, status=400 if answer.get("error") else 200)


@api_view(["GET"])
@permission_classes([IsAdmin])
def channel_manager_reading(request):
    """How the reading of guides is going, for whichever page asked for it."""
    return JsonResponse({"reading": channel_manager.reading_state()})


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
