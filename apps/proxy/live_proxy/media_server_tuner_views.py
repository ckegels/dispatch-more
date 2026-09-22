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
from core.utils import RedisClient

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

    The guide points at the original logo addresses by default ("cachedlogos=false").

    Dispatcharr's cached logos are on its own address, which is usually a private one. The
    media server hands that address to whatever is watching rather than fetching the image
    itself, so a phone or a television off the network cannot load it and the channel shows
    no logo. An address the provider serves on the public internet works everywhere. Anyone
    whose Dispatcharr is reachable from outside can turn this off when adding the tuner.
    """
    url = f"{media_servers.clean_url(base_url)}/output/epg/{quote(channel_profile, safe='')}"
    return f"{url}?cachedlogos=false" if skip_cached_logos else url


def _profile_from_uri(uri) -> str:
    """The channel profile a tuner of ours serves, from its address."""
    parts = [part for part in str(uri).split("/") if part]
    if "hdhr" not in parts:
        return ""
    return unquote(parts[parts.index("hdhr") + 1]) if len(parts) > parts.index("hdhr") + 1 else ""


def _tuner_count_in(uri):
    """How many tuners an address of ours offers, which is written into it."""
    parts = [part for part in str(uri).split("?", 1)[0].split("/") if part]
    if "tuners" not in parts:
        return None
    index = parts.index("tuners") + 1
    try:
        return int(parts[index]) if len(parts) > index else None
    except (TypeError, ValueError):
        return None


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
        "provider_streams": _provider_streams(),
    }


def _provider_streams() -> int:
    """
    How many streams the providers actually allow at once, added up.

    This is what a tuner count is really promising. Told more than this, a media server
    starts streams the providers refuse, and the viewer gets an error from the server
    instead of whatever the channel was going to fall back to. Told fewer, the server holds
    requests back that would have worked.

    Zero when an account has no limit set, which means "as many as you like" and so cannot
    be added up; the page then says nothing rather than warning about a number it invented.
    """
    from apps.m3u.models import M3UAccountProfile

    try:
        streams = 0
        for profile in M3UAccountProfile.objects.filter(
            is_active=True, m3u_account__is_active=True
        ).only("max_streams"):
            if not profile.max_streams:
                # One account without a limit makes the total meaningless
                return 0
            streams += profile.max_streams
        return streams
    except Exception:
        return 0


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

    if action == "set_uri":
        # Changing where a tuner points, without taking it out of its DVR and losing the
        # channels mapped against it
        device_id = str(request.data.get("id") or "")
        uri = media_servers.clean_url(request.data.get("uri"))
        if not uri.startswith(("http://", "https://")):
            return JsonResponse(
                {"error": "The tuner address must start with http:// or https://"}, status=400
            )
        device = next(
            (t for t in media_servers.tuners(server, hosts) if t["id"] == device_id), None
        )
        if device is None:
            return JsonResponse({"error": "That tuner is not on this server"}, status=400)
        # On a tuner of ours the number of tuners is part of the address, so it arrives here
        wanted_tuners = _tuner_count_in(uri)
        if wanted_tuners and wanted_tuners > MAX_TUNERS:
            return JsonResponse(
                {"error": f"Give a number of tuners between 1 and {MAX_TUNERS}"}, status=400
            )
        warning = ""
        if media_servers.set_tuner_uri(
            server, device_id, uri, device.get("title"), wanted_tuners
        ):
            # An address that changed means different channels behind it: without a rescan
            # the server keeps the ones it found at the old one
            if device.get("dvr_id"):
                media_servers.sync_tuner(server, device_id, device["dvr_id"])
        else:
            # Plex keeps the address it had, whatever it is asked. The only way to move a
            # tuner there is to put one at the new address in its place, which is what its
            # own settings do, so that is done rather than refusing.
            profile = _profile_from_uri(uri)
            moved, warning = media_servers.move_tuner(
                server,
                device,
                uri,
                _epg_url(base_url, profile, skip_cached_logos) if profile else None,
                device.get("title") or profile,
                wanted_tuners,
                RedisClient.get_client(),
            )
            if not moved:
                return JsonResponse({"error": warning}, status=400)
        logger.info(f"Tuner {device_id} on {server.get('name')} now points at {uri}")
        return JsonResponse({
            "tuners": media_servers.tuners(server, hosts),
            "dvrs": media_servers.dvr_list(server),
            # Something worked but not everything: worth saying, without looking like a
            # failure the page should undo
            **({"warning": warning} if warning else {}),
            **_choices(),
        })

    if action == "place":
        # Put a tuner where it can be watched. There is nothing to choose: it goes into the
        # DVR the server has, and one is made only when it has none.
        device_id = str(request.data.get("id") or "")
        device = next(
            (t for t in media_servers.tuners(server, hosts) if t["id"] == device_id), None
        )
        if device is None:
            return JsonResponse({"error": "That tuner is not on this server"}, status=400)

        profile = _profile_from_uri(device["uri"])
        guide = (request.data.get("guide_url") or "").strip() or (
            _epg_url(base_url, profile, skip_cached_logos) if profile else ""
        )
        if not guide:
            return JsonResponse(
                {
                    "error": "This tuner is not one of Dispatcharr's, so there is no guide "
                    "to go with it. Put it in the DVR from the server's own settings."
                },
                status=400,
            )

        dvr_id = media_servers.the_dvr(server)
        if dvr_id:
            # The guide first, then the tuner, which is the order the server's settings use
            if not media_servers.add_lineup(server, dvr_id, guide, profile):
                return JsonResponse(
                    {"error": "The server would not add this tuner's guide to its DVR"},
                    status=400,
                )
            media_servers.name_device(server, device_id, device.get("title") or profile)
            if not media_servers.attach_tuner(server, dvr_id, device_id):
                return JsonResponse(
                    {"error": "The server would not put this tuner in its DVR"}, status=400
                )
        else:
            media_servers.name_device(server, device_id, device.get("title") or profile)
            if not media_servers.create_dvr(
                server,
                device["uuid"],
                guide,
                profile,
                language_code(request.data.get("language")),
            ):
                return JsonResponse(
                    {"error": "The server would not make a DVR for this tuner"}, status=400
                )
            dvr_id = media_servers.the_dvr(server)

        if dvr_id:
            media_servers.sync_tuner(server, device_id, dvr_id)
        logger.info(f"Put tuner {device_id} in the DVR on {server.get('name')}")
        return JsonResponse({
            "tuners": media_servers.tuners(server, hosts),
            "dvrs": media_servers.dvr_list(server),
            **_choices(),
        })

    if action == "set_guide":
        # Changing the guide a DVR holds. Making one is not offered: a tuner goes into the
        # DVR the server has, and "place" makes one only when there is none.
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
            # Recent Plex versions answer most DVR writes with "not found", so this is
            # as likely to mean "this server does not allow it" as "that was wrong".
            return JsonResponse(
                {
                    "error": "The server would not change the guide. Newer Plex versions "
                    "refuse this, and the guide then has to be changed in the server's "
                    "own Live TV settings."
                },
                status=400,
            )
        logger.info(f"Changed the guide on DVR {dvr_id} to {guide}")
        # A new guide is not read until the DVR reloads it, so the change would appear to
        # have done nothing until something else happened to make the server look
        media_servers.reload_guide(server, dvr_id)
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
            # The guide goes in before the tuner, which is the order the server's own
            # settings use: a DVR holds a lineup per channel source, and a tuner arriving
            # without one has its channels listed against nothing.
            tuner = next(
                (t for t in media_servers.tuners(server, hosts) if t["id"] == str(device_id)),
                None,
            )
            profile = _profile_from_uri(tuner["uri"]) if tuner else ""
            if profile and not media_servers.add_lineup(
                server, dvr_id, _epg_url(base_url, profile, skip_cached_logos), profile
            ):
                return JsonResponse(
                    {"error": "The server would not add this tuner's guide to that DVR"},
                    status=400,
                )
            if profile and tuner:
                # Without a name the server shows it as a blank row, and a tuner it does
                # not consider enabled is not used
                media_servers.name_device(server, device_id, tuner.get("title") or profile)
            if not media_servers.attach_tuner(server, dvr_id, device_id):
                return JsonResponse(
                    {"error": "The server would not put this tuner in that DVR"}, status=400
                )
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
            # A scan is started, not done, by the time the server answers, so the usual
            # reason for getting here is that it had not found its channels yet
            return JsonResponse(
                {
                    "error": "The rescan did not finish: the server may not have found this "
                    "tuner's channels yet, or would not switch them on. Press Sync again in "
                    "a moment."
                },
                status=400,
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
    # Nothing to choose: the tuner goes into the DVR the server has, and one is made only
    # when it has none. A second DVR is accepted by the server and then never shown.
    dvr_id = media_servers.the_dvr(server) or None
    if device is None:
        warning = "The tuner was added, but the server did not list it afterwards."
    else:
        # A tuner added through the API arrives with no name and switched off, which the
        # server shows as a blank row it will not use
        media_servers.name_device(server, device["id"], channel_profile)

    if device is None:
        pass
    elif dvr_id:
        # Its guide first, then the tuner: a DVR holds a lineup per channel source
        media_servers.add_lineup(
            server,
            dvr_id,
            _epg_url(base_url, channel_profile, skip_cached_logos),
            channel_profile,
        )
        if not media_servers.attach_tuner(server, dvr_id, device["id"]):
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
