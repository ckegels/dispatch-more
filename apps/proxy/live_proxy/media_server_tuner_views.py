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
import re
from urllib.parse import quote, unquote

from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.permissions import IsAdmin

from . import media_servers
from .hdhr_tuner_views import MAX_TUNERS

logger = logging.getLogger("live_proxy")

# A channel profile built here is named so it is obvious where it came from and what it is for
PROFILE_PREFIX = "plexmedia"

# A media server wants a three letter language code, and not always the obvious one ("fre" for
# French, not "fra"). These are the ones a guide is likely to be in, by what people type.
LANGUAGES = [
    ("eng", "English", ("en", "eng")),
    ("fre", "French", ("fr", "fra", "fre", "fr-fr")),
    ("ger", "German", ("de", "deu", "ger")),
    ("dut", "Dutch", ("nl", "nld", "dut")),
    ("spa", "Spanish", ("es", "spa")),
    ("ita", "Italian", ("it", "ita")),
    ("por", "Portuguese", ("pt", "por")),
    ("pol", "Polish", ("pl", "pol")),
    ("tur", "Turkish", ("tr", "tur")),
    ("ara", "Arabic", ("ar", "ara")),
    ("rus", "Russian", ("ru", "rus")),
    ("swe", "Swedish", ("sv", "swe")),
    ("nor", "Norwegian", ("no", "nor")),
    ("dan", "Danish", ("da", "dan")),
    ("fin", "Finnish", ("fi", "fin")),
    ("cze", "Czech", ("cs", "ces", "cze")),
    ("gre", "Greek", ("el", "ell", "gre")),
    ("heb", "Hebrew", ("he", "heb")),
    ("hin", "Hindi", ("hi", "hin")),
    ("chi", "Chinese", ("zh", "zho", "chi")),
    ("jpn", "Japanese", ("ja", "jpn")),
]


def language_code(typed) -> str:
    """
    What the media server should be given for a language someone typed.

    "fr", "fra" and "French" all mean the same thing to a person and only one of them means
    it to the server, so anything recognisable is turned into the server's code and anything
    else is passed on as it was typed.
    """
    typed = (typed or "").strip().lower()
    if not typed:
        return "eng"
    for code, name, aliases in LANGUAGES:
        if typed == code or typed == name.lower() or typed in aliases:
            return code
    return typed


def _server(server_id):
    return next(
        (s for s in media_servers.load_servers() if s.get("id") == server_id), None
    )


def default_base_url(request) -> str:
    """
    A first guess at the address a media server can reach this Dispatcharr on: the one the
    browser is using. Behind a proxy the port is often missing from the Host header, so it is
    taken from the forwarded port when there is one. It is only a guess, and the tab shows it
    as a field that can be corrected.
    """
    base = request.build_absolute_uri("/").rstrip("/")
    forwarded_port = request.META.get("HTTP_X_FORWARDED_PORT")
    if forwarded_port and ":" not in base.split("//", 1)[-1]:
        base = f"{base}:{forwarded_port}"
    return base


def _tuner_url(
    base_url, channel_profile, output_profile_id=None, tuner_count=None, tuner_type="hdhomerun"
):
    """
    This Dispatcharr as an HDHomeRun for that channel profile.

    With a tuner count it goes to /proxy/hdhr/..., which says how many streams the media
    server may start at once instead of counting custom streams (see hdhr_tuner_views).
    """
    # A profile name may contain spaces and other characters that cannot go in an address
    base = media_servers.clean_url(base_url)
    profile = quote(channel_profile, safe="")
    if tuner_type == "m3u":
        # A playlist instead of a tuner: the same channels, without the tuner count
        return f"{base}/output/m3u/{profile}"
    url = f"{base}/proxy/hdhr/{profile}" if tuner_count else f"{base}/hdhr/{profile}"
    if output_profile_id:
        url = f"{url}/output_profile/{int(output_profile_id)}"
    if tuner_count:
        url = f"{url}/tuners/{int(tuner_count)}"
    return url


def _epg_url(base_url, channel_profile, skip_cached_logos=True) -> str:
    """
    Dispatcharr's own EPG for a channel profile, which is what a DVR uses as its guide.

    Cached logos usually do not show up on a media server, so by default the guide points at
    the original addresses instead ("cachedlogos=false"). Why they do not is not understood:
    the cache endpoint needs no login and sits on the same network, so the server can reach
    it. Anyone whose server does show them can turn this off when adding the tuner.
    """
    url = f"{media_servers.clean_url(base_url)}/output/epg/{quote(channel_profile, safe='')}"
    return f"{url}?cachedlogos=false" if skip_cached_logos else url


def _profile_from_uri(uri) -> str:
    """The channel profile a tuner of ours serves, from its address."""
    parts = [part for part in str(uri).split("/") if part]
    if "hdhr" not in parts:
        return ""
    return unquote(parts[parts.index("hdhr") + 1]) if len(parts) > parts.index("hdhr") + 1 else ""


def _profile_from_guide(url) -> str:
    """
    The channel profile a guide of ours covers, from its address.

    Used as the name a DVR is stored under, so a DVR says which channels it lists rather
    than being called after the language it happens to be in.
    """
    path = str(url).split("?", 1)[0]
    parts = [part for part in path.split("/") if part]
    if "epg" not in parts:
        return ""
    index = parts.index("epg") + 1
    return unquote(parts[index]) if len(parts) > index else ""


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
        "max_tuners": MAX_TUNERS,
        "languages": [
            {"value": code, "label": f"{name} ({code})"} for code, name, _aliases in LANGUAGES
        ],
        # What Dispatcharr would advertise on its own, so the field can be compared to it
        "calculated_tuners": _calculated_tuners(),
    }


def _calculated_tuners() -> int:
    """
    What Dispatcharr would advertise on its own, shown next to the Tuners field to compare.

    Zero when it cannot be worked out, which the page reads as "nothing to compare with"
    rather than as a real answer: it is only there to help choose a number.
    """
    from apps.m3u.utils import calculate_tuner_count

    try:
        return calculate_tuner_count(minimum=1, unlimited_default=10)
    except Exception:
        return 0


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
    base_url = server.get("dispatcharr_url") or default_base_url(request)
    if request.method == "GET":
        return JsonResponse({
            "tuners": media_servers.tuners(server, hosts),
            "dvrs": media_servers.dvr_list(server),
            "base_url": base_url,
            "kind": media_servers.kind(server),
            **_choices(),
        })

    # Media servers cannot read Dispatcharr's cached logos, so this is on unless it is turned
    # off when adding the tuner
    skip_cached_logos = request.data.get("skip_cached_logos", True) is not False

    if request.method == "DELETE" and request.query_params.get("dvr"):
        dvr_id = request.query_params.get("dvr")
        if not media_servers.delete_dvr(server, dvr_id):
            return JsonResponse({"error": "The server refused to remove that DVR"}, status=400)
        logger.info(f"Removed DVR {dvr_id} from media server {server.get('name')}")
        return JsonResponse({
            "tuners": media_servers.tuners(server, hosts),
            "dvrs": media_servers.dvr_list(server),
            **_choices(),
        })

    if request.method == "DELETE":
        device_id = request.query_params.get("id")
        if not media_servers.delete_tuner(server, device_id):
            return JsonResponse({"error": "The server refused to remove that tuner"}, status=400)
        logger.info(f"Removed tuner {device_id} from media server {server.get('name')}")
        return JsonResponse({
            "tuners": media_servers.tuners(server, hosts),
            "dvrs": media_servers.dvr_list(server),
            **_choices(),
        })

    action = request.data.get("action") or "add"

    if action in ("make_dvr", "set_guide"):
        # The guide belongs with the tuner: a DVR is a tuner plus the guide its channels are
        # listed in. Both are given here so neither can be left behind, which is what made a
        # tuner sit in a DVR playing channels with no programmes against them.
        guide = (request.data.get("guide_url") or "").strip()
        if not guide.startswith(("http://", "https://")):
            return JsonResponse(
                {"error": "The guide address must start with http:// or https://"}, status=400
            )
        title = (
            (request.data.get("title") or "").strip()
            or _profile_from_guide(guide)
            or "Dispatcharr"
        )

        if action == "set_guide":
            dvr_id = request.data.get("dvr_id")
            dvr = next(
                (d for d in media_servers.dvr_list(server) if d["id"] == str(dvr_id)), None
            )
            if dvr is None:
                return JsonResponse({"error": "That DVR is not on this server"}, status=400)
            # Its tuners go back with the guide: a DVR is stored as both at once
            if not media_servers.set_guide(
                server, dvr_id, guide, title, dvr.get("devices") or ()
            ):
                return JsonResponse(
                    {"error": "The server would not change the guide on that DVR"}, status=400
                )
            logger.info(f"Changed the guide on DVR {dvr_id} to {guide}")
        else:
            device_id = str(request.data.get("id") or "")
            device = next(
                (t for t in media_servers.tuners(server, hosts) if t["id"] == device_id), None
            )
            if device is None:
                return JsonResponse({"error": "That tuner is not on this server"}, status=400)
            if device.get("dvr_id"):
                return JsonResponse(
                    {"error": "This tuner is already in a DVR"}, status=400
                )
            if not media_servers.create_dvr(
                server,
                device["uuid"],
                guide,
                title,
                language_code(request.data.get("language")),
            ):
                return JsonResponse(
                    {"error": "The server would not make a DVR for this tuner"}, status=400
                )
            logger.info(f"Made a DVR for tuner {device_id} with guide {guide}")

        return JsonResponse({
            "tuners": media_servers.tuners(server, hosts),
            "dvrs": media_servers.dvr_list(server),
            **_choices(),
        })

    if action in ("sync", "attach"):
        device_id = request.data.get("id")
        dvr_id = request.data.get("dvr_id") or None

        if action == "attach":
            if not dvr_id:
                return JsonResponse({"error": "Choose a DVR to put it in"}, status=400)
            if not media_servers.attach_tuner(server, dvr_id, device_id):
                return JsonResponse(
                    {"error": "The server would not put this tuner in that DVR"}, status=400
                )
            # Nothing else to do about the guide: a DVR keeps one guide, shared by every
            # tuner in it. The tuner has programmes if that guide covers its channels, which
            # is why the page shows the guide against each tuner and lets it be changed.
        elif not dvr_id:
            # Nothing to rescan: a tuner outside a DVR is not used by the server at all
            return JsonResponse(
                {
                    "error": "This tuner is not in a DVR yet, so there is nothing to rescan. "
                    "Put it in a DVR first."
                },
                status=400,
            )
        elif not media_servers.sync_tuner(server, device_id, dvr_id):
            return JsonResponse(
                {"error": "The server refused to rescan this tuner"}, status=400
            )
        return JsonResponse({
            "tuners": media_servers.tuners(server, hosts),
            "dvrs": media_servers.dvr_list(server),
            **_choices(),
        })

    # Adding a tuner: an existing channel profile, or one built from channel groups
    channel_profile = (request.data.get("channel_profile") or "").strip()
    group_ids = request.data.get("group_ids") or []
    new_profile_name = (request.data.get("new_profile_name") or "").strip()
    built = None
    if not channel_profile and new_profile_name:
        try:
            channel_profile = built = _build_profile(new_profile_name, group_ids)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
    if not channel_profile:
        return JsonResponse({"error": "Choose a channel profile, or build one"}, status=400)

    # The address the media server will use. Remembered on the server it was added to, so it
    # does not have to be corrected again for the next tuner.
    base_url = media_servers.clean_url(request.data.get("base_url")) or base_url
    if not base_url.startswith(("http://", "https://")):
        return JsonResponse(
            {"error": "The Dispatcharr address must start with http:// or https://"}, status=400
        )
    if base_url != server.get("dispatcharr_url"):
        servers = media_servers.load_servers()
        for stored in servers:
            if stored.get("id") == server.get("id"):
                stored["dispatcharr_url"] = base_url
        media_servers.save_servers(servers)

    try:
        tuner_count = int(request.data.get("tuner_count") or 0)
    except (TypeError, ValueError):
        tuner_count = 0
    if tuner_count < 0 or tuner_count > MAX_TUNERS:
        return JsonResponse(
            {"error": f"Give a number of tuners between 1 and {MAX_TUNERS}"}, status=400
        )

    tuner_type = "m3u" if request.data.get("tuner_type") == "m3u" else "hdhomerun"
    uri = _tuner_url(
        base_url,
        channel_profile,
        request.data.get("output_profile_id"),
        tuner_count,
        tuner_type,
    )
    if not media_servers.add_tuner(server, uri, channel_profile, tuner_count, tuner_type):
        # A profile built for a tuner that was refused would be left behind with no way
        # to reach it, so it goes again and the next try starts clean.
        if built:
            _delete_profile(built)
        return JsonResponse(
            {"error": f"The server could not add a tuner at {uri}"}, status=400
        )
    logger.info(f"Added tuner {uri} to media server {server.get('name')}")

    # A tuner on its own is registered and unused. Put it where it can be watched: in the DVR
    # that was chosen, or in a new one with Dispatcharr's own EPG as its guide.
    warning = ""
    device = next(
        (t for t in media_servers.tuners(server, hosts) if t["uri"] == uri), None
    )
    dvr_id = request.data.get("dvr_id") or None
    if device is None:
        warning = "The tuner was added, but the server did not list it afterwards."
    elif dvr_id:
        if media_servers.attach_tuner(server, dvr_id, device["id"]):
            # The DVR's own guide now covers this tuner as well. If it does not reach these
            # channels the tuner plays with no programmes, which the page says and can fix.
            pass
        else:
            warning = "The tuner was added, but the server would not put it in that DVR."
    else:
        if media_servers.create_dvr(
            server,
            device["uuid"],
            _epg_url(base_url, channel_profile, skip_cached_logos),
            channel_profile,
            language_code(request.data.get("language")),
        ):
            dvr_id = media_servers.dvr_for_device(server, device["id"])
        else:
            warning = (
                "The tuner was added, but the server would not make a DVR for it. "
                "Put it in a DVR yourself, or choose an existing one."
            )

    if device is not None and dvr_id:
        # Scan its channels and load the guide, so it is ready to watch
        media_servers.sync_tuner(server, device["id"], dvr_id)

    return JsonResponse({
        "tuners": media_servers.tuners(server, hosts),
        "dvrs": media_servers.dvr_list(server),
        "warning": warning,
        **_choices(),
    })


def _delete_profile(name):
    """Remove a channel profile this built (its memberships go with it)."""
    from apps.channels.models import ChannelProfile

    ChannelProfile.objects.filter(name=name).delete()


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
    # "PlexMedia France" must not become "plexmedia-PlexMedia France", and the name ends up in
    # the tuner's address, so spaces become dashes: "plexmedia-France".
    if name.lower().startswith(PROFILE_PREFIX):
        name = name[len(PROFILE_PREFIX) :].lstrip(" -")
    name = re.sub(r"[^\w.-]+", "-", name, flags=re.UNICODE).strip("-")
    name = f"{PROFILE_PREFIX}-{name}".rstrip("-")
    if ChannelProfile.objects.filter(name=name).exists():
        raise ValueError(
            f"A channel profile called {name} already exists: choose it above, or remove it"
        )

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
