"""Tuners on a media server: what is there, adding one, removing one, and syncing it.

Dispatcharr already pretends to be an HDHomeRun at /hdhr/<channel profile>, optionally with an
output profile, so adding a tuner is telling the media server that address. What this adds is
doing it from the settings instead of from the server's own wizard, seeing what is really
there (including tuners left over from earlier setups), and building a channel profile out of
channel groups so a tuner shows only what belongs on it.

Writes to the media server are limited to tuners: adding, removing, rescanning and reloading
the guide. Creating the DVR itself stays where it is, in the server's own settings.
"""

import logging

from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.permissions import IsAdmin

from . import media_servers

logger = logging.getLogger("live_proxy")

# A channel profile built here is named so it is obvious where it came from and what it is for
PROFILE_PREFIX = "plexmedia"


def _server(server_id):
    return next(
        (s for s in media_servers.load_servers() if s.get("id") == server_id), None
    )


def _tuner_url(request, channel_profile, output_profile_id=None):
    """
    The address of this Dispatcharr as an HDHomeRun for that channel profile.

    Built from the address the browser is talking to, which is the one the media server can
    reach as well in a normal setup; it is shown before it is used so it can be corrected.
    """
    base = request.build_absolute_uri("/").rstrip("/")
    url = f"{base}/hdhr/{channel_profile}"
    if output_profile_id:
        url = f"{url}/output_profile/{int(output_profile_id)}"
    return url


def _choices():
    """What a tuner can be built from: channel profiles, groups and output profiles."""
    from django.db.models import Count

    from apps.channels.models import ChannelGroup, ChannelProfile
    from core.models import OutputProfile

    groups = (
        ChannelGroup.objects.annotate(how_many=Count("channels"))
        .filter(how_many__gt=0)
        .order_by("name")
    )
    return {
        "channel_profiles": [
            {"id": profile.id, "name": profile.name}
            for profile in ChannelProfile.objects.order_by("name")
        ],
        "channel_groups": [
            {"id": group.id, "name": group.name, "channels": group.how_many}
            for group in groups
        ],
        "output_profiles": [
            {"id": profile.id, "name": profile.name}
            for profile in OutputProfile.objects.filter(is_active=True).order_by("name")
        ],
        "profile_prefix": PROFILE_PREFIX,
    }


@api_view(["GET", "POST", "DELETE"])
@permission_classes([IsAdmin])
def media_server_tuners(request):
    """The tuners on a server, and adding, removing or syncing one."""
    server_id = request.query_params.get("server") or request.data.get("server")
    server = _server(server_id)
    if server is None:
        return JsonResponse({"error": "No such media server"}, status=404)
    if not media_servers.is_enabled(server):
        return JsonResponse({"error": "This media server is switched off"}, status=400)

    hosts = media_servers.server_hosts()
    if request.method == "GET":
        return JsonResponse({
            "tuners": media_servers.tuners(server, hosts),
            **_choices(),
        })

    if request.method == "DELETE":
        device_id = request.query_params.get("id")
        if not media_servers.delete_tuner(server, device_id):
            return JsonResponse({"error": "The server refused to remove that tuner"}, status=400)
        logger.info(f"Removed tuner {device_id} from media server {server.get('name')}")
        return JsonResponse({"tuners": media_servers.tuners(server, hosts), **_choices()})

    action = request.data.get("action") or "add"
    if action == "sync":
        device_id = request.data.get("id")
        dvr_id = request.data.get("dvr_id") or None
        if not media_servers.sync_tuner(server, device_id, dvr_id):
            return JsonResponse(
                {"error": "The server refused to rescan this tuner"}, status=400
            )
        return JsonResponse({"tuners": media_servers.tuners(server, hosts), **_choices()})

    # Adding a tuner: an existing channel profile, or one built from channel groups
    channel_profile = (request.data.get("channel_profile") or "").strip()
    group_ids = request.data.get("group_ids") or []
    new_profile_name = (request.data.get("new_profile_name") or "").strip()
    if not channel_profile and new_profile_name:
        try:
            channel_profile = _build_profile(new_profile_name, group_ids)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
    if not channel_profile:
        return JsonResponse({"error": "Choose a channel profile, or build one"}, status=400)

    uri = _tuner_url(request, channel_profile, request.data.get("output_profile_id"))
    if not media_servers.add_tuner(server, uri):
        return JsonResponse(
            {"error": f"The server could not add a tuner at {uri}"}, status=400
        )
    logger.info(f"Added tuner {uri} to media server {server.get('name')}")
    return JsonResponse({"tuners": media_servers.tuners(server, hosts), **_choices()})


def _build_profile(name, group_ids) -> str:
    """
    A channel profile holding only the channels of the chosen groups.

    Dispatcharr normally fills a new profile with every channel; this one starts empty
    (_start_empty) so a tuner for one group does not offer the whole lineup, and nothing is
    added to any other profile.
    """
    from apps.channels.models import Channel, ChannelProfile, ChannelProfileMembership

    if not group_ids:
        raise ValueError("Choose at least one channel group")
    name = name if name.startswith(f"{PROFILE_PREFIX}-") else f"{PROFILE_PREFIX}-{name}"
    if ChannelProfile.objects.filter(name=name).exists():
        raise ValueError(f"A channel profile called {name} already exists")

    profile = ChannelProfile(name=name)
    profile._start_empty = True
    profile.save()
    channels = Channel.objects.filter(channel_group_id__in=group_ids)
    ChannelProfileMembership.objects.bulk_create(
        [
            ChannelProfileMembership(channel_profile=profile, channel=channel)
            for channel in channels
        ],
        ignore_conflicts=True,
    )
    logger.info(f"Built channel profile {name} with {channels.count()} channel(s)")
    return name
