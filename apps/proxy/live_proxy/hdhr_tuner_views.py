"""An HDHomeRun that says how many tuners it has, instead of working it out.

Dispatcharr calculates the tuner count it advertises from the M3U profiles plus the number of
custom streams (apps.m3u.utils.calculate_tuner_count). With a custom fallback stream per
channel that number becomes enormous -- 1362 on a setup whose providers allow two streams --
and a media server believes it, starts as many streams as it likes, and the viewer gets errors
instead of the server waiting its turn.

These endpoints are the same HDHomeRun, at /proxy/hdhr/<channel profile>/tuners/<n>/, with the
tuner count taken from the address. The lineup comes from Dispatcharr's own views, unchanged:
only the count in discover.json is ours, because that is the only part that is wrong.
"""

import logging

from django.http import JsonResponse

from apps.hdhr.api_views import (
    DiscoverAPIView,
    HDHRDeviceXMLAPIView,
    LineupAPIView,
    LineupStatusAPIView,
)
from core.utils import build_absolute_uri_with_port

logger = logging.getLogger("live_proxy")

# A media server is told the count once, when the tuner is added, so it has to be sensible
# rather than generous: it is the number of streams it may start at the same time.
MAX_TUNERS = 64


def _base_url(request, channel_profile, tuner_count, output_profile_id=None):
    """
    The address of this tuner, as the media server should keep using it.

    Built from the same parts as the address it was reached on, so discover.json points at
    itself and not at the counting version of the same lineup.
    """
    parts = ["proxy", "hdhr", channel_profile]
    if output_profile_id is not None:
        parts += ["output_profile", str(output_profile_id)]
    parts += ["tuners", str(tuner_count)]
    return build_absolute_uri_with_port(request, f"/{'/'.join(parts)}/").rstrip("/")


def hdhr_document(request, channel_profile, tuner_count, document, output_profile_id=None):
    """discover.json, lineup.json, lineup_status.json or device.xml for this tuner."""
    handler = {
        "discover.json": discover,
        "lineup.json": lineup,
        "lineup_status.json": lineup_status,
        "device.xml": device_xml,
    }.get(document)
    if handler is None:
        return JsonResponse({"error": "Not found"}, status=404)
    return handler(request, channel_profile, tuner_count, output_profile_id)


def discover(request, channel_profile, tuner_count, output_profile_id=None):
    """discover.json, with the tuner count from the address."""
    tuner_count = max(1, min(int(tuner_count), MAX_TUNERS))
    # Everything except the count (and the address) is Dispatcharr's own answer
    original = DiscoverAPIView.as_view()(
        request, channel_profile=channel_profile, output_profile_id=output_profile_id
    )
    if original.status_code != 200:
        return original

    import json

    data = json.loads(original.content)
    base_url = _base_url(request, channel_profile, tuner_count, output_profile_id)
    data.update({
        "TunerCount": tuner_count,
        "BaseURL": base_url,
        "LineupURL": f"{base_url}/lineup.json",
        # Its own device, so it cannot be confused with the same profile added the usual way
        "DeviceID": f"{data.get('DeviceID', 'dispatcharr-hdhr')}-t{tuner_count}",
        "FriendlyName": f"{data.get('FriendlyName', 'Dispatcharr HDHomeRun')} ({tuner_count} tuners)",
    })
    return JsonResponse(data)


def lineup(request, channel_profile, tuner_count, output_profile_id=None):
    """The channels, exactly as Dispatcharr serves them."""
    return LineupAPIView.as_view()(
        request, channel_profile=channel_profile, output_profile_id=output_profile_id
    )


def lineup_status(request, channel_profile, tuner_count, output_profile_id=None):
    return LineupStatusAPIView.as_view()(
        request, channel_profile=channel_profile, output_profile_id=output_profile_id
    )


def device_xml(request, channel_profile, tuner_count, output_profile_id=None):
    return HDHRDeviceXMLAPIView.as_view()(request)
